import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  type BridgeRealizationView,
  ENTITY_BRIDGE_REJECT_CATEGORIES,
  type EntityBridgeRejectCategory,
  type GovernanceQueue,
  type GovernanceQueueItem,
  type GovernanceQueueUsage,
  REJECT_CATEGORIES,
  type RejectCategory,
  TABLE_FACT_REJECT_CATEGORIES,
  type TableFactRejectCategory,
  bulkRejectEntityBridges,
  confirmEntityBridge,
  confirmJoin,
  confirmSemanticBinding,
  SEMANTIC_BINDING_REJECT_CATEGORIES,
  type SemanticBindingRejectCategory,
  confirmTableFact,
  getGovernanceQueue,
  rejectEntityBridge,
  rejectJoin,
  rejectSemanticBinding,
  rejectTableFact,
  reviewBridgeRealization,
} from '../api'
import { ConceptConfirmationPanel } from './ConceptConfirmationPanel'
import { DataUsePolicyPanel } from './DataUsePolicyPanel'

// GOVERNANCE REVIEW — a decision queue, not a search box.
//
// This screen used to open on a text input labelled "source" and render nothing until the operator
// guessed a catalog slug (the live ones are `cib` and `ftr` — neither discoverable nor memorable).
// It now opens on GET /governance/queue: one request, no source argument, every pending decision
// across every catalog the caller may see. There is no input to fill in and nothing to press first.
//
// ── THREE AXES, RENDERED SEPARATELY, NEVER FUSED ─────────────────────────────────────────────────
//
//   1. LINK AVAILABILITY          — may the platform consider this link at all?
//   2. AUTOMATIC EXECUTION SAFETY — has the directional join / cardinality / fan-out been validated
//                                   automatically? (`production_eligibility`)
//   3. HUMAN REVIEW               — has a person endorsed the semantics? (`state`)
//
// Human review controls NEITHER of the other two. A row can legitimately be unreviewed AND
// production-eligible, or human-endorsed AND sandbox-only, so the two payload code fields render as
// two distinct signals with their own labels, their own tone and their own `data-code`. Merging them
// into one badge would make one of those two ordinary rows unrenderable without lying.
//
// Every signal is bound to a `*_code` field, never to the prose: the labels are display text the
// backend may reword, the codes are the contract. Axis 1 is the one derivation this screen makes —
// the read model folds availability into the `state` label rather than shipping a field for it — so
// it is mapped from the known `state_code` values and says NOTHING for a code it does not know.
//
// CONFIRMATION MEANS "a human agrees with this semantic relationship". It is never permission to
// execute: a proposed link is already consumed, and eligibility to run is axis 2, which moves on its
// own. Nothing here may suggest that a review unlocks or releases anything.
//
// ── WHAT THE SERVER DECIDES AND THIS SCREEN ONLY RENDERS ─────────────────────────────────────────
//
// `available_actions` is the caller's sanctioned set with four-eyes already applied, so button
// enablement is read from it and never computed here. A row missing `confirm` renders it DISABLED
// with the reason on screen and wired via aria-describedby. A row with no actions at all is an
// endorsed fact whose re-verification has its own flow — the queue must not offer a button the
// command layer would refuse.
//
// ── USAGE: "already depended on by" ──────────────────────────────────────────────────────────────
//
// Each category answers counted / not_tracked_yet / unreadable, and an unmeasurable category renders
// as the WORDS "not tracked yet" — never 0, never blank, never omitted. `count` is null unless the
// state is `counted`, so there is no path that turns "nothing was recorded" into a number. Joins and
// table facts have no bridge anchor and carry NO usage at all: their block is absent rather than
// rendered as zero dependencies.
//
// DELIBERATELY NOT HERE (both need a per-catalog surface the queue endpoint does not serve): the
// governed-join divergence banner and the per-table relationship-readiness diagnostic.

// ── vocabulary ───────────────────────────────────────────────────────────────────────────────────

// Business words for each decision kind. An unknown kind from a newer backend still renders (its
// code, de-underscored) rather than breaking the client.
const KIND_LABEL: Record<string, string> = {
  entity_bridge: 'Cross-catalog identifier links',
  approved_join: 'Discovered joins',
  grain: 'Table grain',
  availability_time: 'As-of date',
  currency_binding: 'Currency bindings',
  entity_assignment: 'Entity assignments',
}
const KIND_LABEL_ONE: Record<string, string> = {
  entity_bridge: 'Cross-catalog identifier link',
  approved_join: 'Discovered join',
  grain: 'Table grain',
  availability_time: 'As-of date',
  currency_binding: 'Currency binding',
  entity_assignment: 'Entity assignment',
}
// What each kind IS, for the section lead-in — including the fact that a discovered join lives
// inside ONE catalog: `list_open_approved_join_proposals` filters on `from_ref.catalog_source` only
// and is not endpoint-symmetric, so a join must never be presented as a cross-catalog finding.
const KIND_ABOUT: Record<string, string> = {
  entity_bridge: 'The same business entity in two different catalogs. These are the decisions no '
    + 'per-catalog screen could show you.',
  approved_join: 'A join between two tables inside one catalog, discovered from metadata and '
    + 'listed under the catalog it starts in.',
  grain: 'What one row of a table is — the key every feature on it aggregates to.',
  availability_time: 'Which column carries the as-of date point-in-time features read.',
  currency_binding: 'What currency an amount column is in — a fixed code, or the column on the '
    + 'same table that names it per row. Features that sum money are refused until this is decided.',
  entity_assignment: 'Which business entity an identifier column denotes.',
}

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind.replaceAll('_', ' ')
}

function kindLabelOne(kind: string): string {
  return KIND_LABEL_ONE[kind] ?? kind.replaceAll('_', ' ')
}

function categoryLabel(category: string): string {
  return category.replaceAll('_', ' ')
}

// No display name exists anywhere in this system: `graph_node` carries no per-catalog label and the
// upload surface takes the slug as the only identifier. Upper-casing the slug is a rendering choice;
// inventing a readable name would be inventing a source of truth.
function catalogLabel(slug: string): string {
  return slug.toUpperCase()
}

// AXIS 3 — the human axis, keyed by `state_code`. Tone only; the words come from the payload.
const REVIEW_TONE: Record<string, string> = {
  unreviewed_available: 'open',
  partially_endorsed_available: 'open',
  human_endorsed: 'ok',
  stale_unavailable: 'warn',
  rejected: 'off',
}

// AXIS 2 — the automatic axis, keyed by `production_eligibility_code`.
const EXECUTION_TONE: Record<string, string> = {
  grain_resolved: 'ok',
  deterministically_validated: 'ok',
  cardinality_unresolved: 'warn',
  cardinality_unknown: 'warn',
  fanout_risk: 'warn',
  realization_not_executable: 'warn',
  not_evaluated: 'quiet',
  not_observed: 'quiet',
  not_applicable: 'quiet',
}

// `production_eligibility` is null when this item kind has nothing to derive the axis from. The
// absence is stated as an absence — never rendered as a pass and never as a failure.
const EXECUTION_ABSENT: Record<string, string> = {
  not_applicable: 'Not applicable — this fact describes one table, so there is no directional join '
    + 'to validate',
  not_observed: 'Not observed — no derivation evidence was recorded for this crossing',
  not_evaluated: 'Not evaluated — no directional realization has been assessed',
}

// AXIS 1 — DERIVED for display from `state_code`, because the read model folds the availability
// consequence into the state label instead of shipping a field for it. These are the states it
// names: DRAFT/PARTIALLY_CONFIRMED and VERIFIED are what the platform will consider, REVERIFY/STALE
// and REJECTED are what it will not. An unrecognized code renders NOTHING rather than a guess.
const AVAILABILITY: Record<string, string> = {
  unreviewed_available: 'The platform may use this link now',
  partially_endorsed_available: 'The platform may use this link now',
  human_endorsed: 'The platform may use this link now',
  stale_unavailable: 'The platform will not consider this link',
  rejected: 'The platform will not consider this link',
}

const AVAILABILITY_TONE: Record<string, string> = {
  unreviewed_available: 'ok',
  partially_endorsed_available: 'ok',
  human_endorsed: 'ok',
  stale_unavailable: 'warn',
  rejected: 'off',
}

// The per-kind reject vocabulary — each command has its own, and `wrong_grain_columns` means nothing
// about an identifier link, so they are never pooled.
function rejectCategories(kind: string): readonly string[] {
  if (kind === 'entity_bridge') return ENTITY_BRIDGE_REJECT_CATEGORIES
  if (kind === 'approved_join') return REJECT_CATEGORIES
  if (kind === 'currency_binding' || kind === 'entity_assignment') {
    return SEMANTIC_BINDING_REJECT_CATEGORIES
  }
  return TABLE_FACT_REJECT_CATEGORIES
}

// Which `unreadable` listings belong to a kind, so an empty section can say "we could not look"
// instead of "nothing is waiting". Both table-fact kinds come from the one `table_fact` listing.
function unreadableListings(kind: string): string[] {
  if (kind === 'grain' || kind === 'availability_time') return ['table_fact']
  if (kind === 'currency_binding' || kind === 'entity_assignment') return ['semantic_binding']
  return [kind]
}

// ── payload readers (detail is an open map: read defensively, never assume) ───────────────────────

function asStr(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function asRec(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {}
}

function asStrArr(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

// The subject in business words, from whatever the kind's payload actually carries. Falls back to
// the read model's own `subject` string, so a kind this screen has never seen still reads.
function headline(item: GovernanceQueueItem): string {
  const d = item.detail
  if (item.kind === 'entity_bridge') {
    const entity = asStr(d.entity_id) || 'entity'
    const where = item.catalogs.map(catalogLabel).join(' and ')
    return where ? `The same ${entity} in ${where}` : `The same ${entity} in two catalogs`
  }
  if (item.kind === 'approved_join') {
    const from = asRec(d.from)
    const to = asRec(d.to)
    if (asStr(from.table) && asStr(to.table)) {
      return `Rows of ${asStr(from.table)} point at ${asStr(to.table)}`
    }
  }
  if (item.kind === 'grain') {
    const columns = asStrArr(asRec(d.proposed_value).columns)
    const table = asStr(d.table)
    if (table && columns.length > 0) return `One row of ${table} is one ${columns.join(' + ')}`
    if (table) return `What one row of ${table} is`
  }
  if (item.kind === 'availability_time') {
    const column = asStr(asRec(d.proposed_value).column)
    const table = asStr(d.table)
    if (table && column) return `${table} is as of ${column}`
    if (table) return `The as-of date of ${table}`
  }
  if (item.kind === 'currency_binding') {
    const column = item.subject.split('.').pop() ?? item.subject
    const fixed = asStr(asRec(d.value).currency_code)
    const target = asStr(asRec(d.target).column)
    if (fixed) return `${column} is always in ${fixed}`
    if (target) return `${column} is in the currency named by ${target}`
    return `The currency of ${column}`
  }
  if (item.kind === 'entity_assignment') {
    const column = item.subject.split('.').pop() ?? item.subject
    const entity = asStr(d.entity_id)
    return entity ? `${column} identifies a ${entity}` : `What ${column} identifies`
  }
  return item.subject
}

// The agreement a confirmation RECORDS, in the words of the thing being agreed to. This is the
// meaning of the act — accountability for a semantic claim — so it is stated per kind, not implied.
function agreement(item: GovernanceQueueItem): string {
  const d = item.detail
  if (item.kind === 'entity_bridge') {
    const entity = asStr(d.entity_id) || 'business entity'
    return `I agree that these two columns identify the same real-world ${entity}.`
  }
  if (item.kind === 'approved_join') {
    const from = asRec(d.from)
    const to = asRec(d.to)
    // Task 5 codegen-review remediation (M3): a blank uploaded cardinality is proposed as null
    // now, and this sentence IS the agreement the confirmation records — it must never assert
    // "the stated cardinality" when none was stated. Confirming the join is still meaningful
    // (the pair and direction), but it stays unusable at runtime until a cardinality exists.
    const cardinality = asStr(d.cardinality)
    const atClause = cardinality
      ? `, at ${cardinality}.`
      : ', at an unstated cardinality — this join stays unusable until one is supplied.'
    return `I agree that ${asStr(from.table) || 'the from table'} joins into `
      + `${asStr(to.table) || 'the to table'} in this direction${atClause}`
  }
  if (item.kind === 'grain') {
    const columns = asStrArr(asRec(d.proposed_value).columns).join(' + ')
    return `I agree that one row of ${asStr(d.table) || 'this table'} is one `
      + `${columns || 'row of the proposed key'}.`
  }
  if (item.kind === 'availability_time') {
    const column = asStr(asRec(d.proposed_value).column) || 'the proposed column'
    return `I agree that ${column} is the as-of date of ${asStr(d.table) || 'this table'}.`
  }
  if (item.kind === 'currency_binding') {
    const fixed = asStr(asRec(item.detail.value).currency_code)
    const target = asStr(asRec(item.detail.target).column)
    if (fixed) return `I agree this amount is always denominated in ${fixed}.`
    if (target) {
      return `I agree this amount's currency varies per row and is named by ${target}.`
    }
    return 'I agree with this currency binding as it is described here.'
  }
  if (item.kind === 'entity_assignment') {
    const entity = asStr(item.detail.entity_id) || 'the proposed entity'
    return `I agree this column identifies a ${entity}.`
  }
  return 'I agree with this relationship as it is described here.'
}

// A table fact carries no proposer: `origin` is a CONSTANT describing how the proposal was made
// (`table_fact_governance._ORIGIN = "llm_proposed_not_profiled"`), and it is the only origin the
// backend emits. Rendering it in the `proposed_by` slot read "Proposed by llm_proposed_not_profiled"
// on every grain and as-of row — a provenance stamp dressed up as a person. It is a METHOD, so it
// is said as one, and an origin this client does not know is de-underscored rather than guessed at.
const ORIGIN_LABEL: Record<string, string> = {
  llm_proposed_not_profiled: 'Proposed automatically, by reading the uploaded schema — not '
    + 'profiled against the data',
}

function originLine(origin: string): string {
  return ORIGIN_LABEL[origin] ?? `Proposed automatically (${origin.replaceAll('_', ' ')})`
}

// Who put it forward and when — strictly from the payload. The bridge listing carries
// proposed_by/proposed_at, the table-fact listing carries an origin, and the join listing carries
// neither (only recorded endorsements), so this line is SHORTER for a join rather than invented.
function provenanceParts(item: GovernanceQueueItem): string[] {
  const d = item.detail
  const parts: string[] = []
  const by = asStr(d.proposed_by)
  if (by) parts.push(`Proposed by ${by}`)
  const origin = asStr(d.origin)
  if (origin) parts.push(originLine(origin))
  const at = asStr(d.proposed_at)
  // Coarse on purpose: the calendar day is the triage signal, not a stopwatch.
  if (at) parts.push(at.slice(0, 10))
  const approvals = Array.isArray(d.approvals) ? d.approvals.map(asRec) : []
  for (const approval of approvals) {
    const who = asStr(approval.display_name) || asStr(approval.subject)
    parts.push(who ? `endorsed by ${who}` : 'one endorsement recorded')
  }
  return parts
}

// The reason `confirm` is not on offer, derived from the SERVER's own decision plus the payload it
// came with — never from a four-eyes rule recomputed here.
function withheldReason(item: GovernanceQueueItem): string {
  if (item.available_actions.length === 0 && item.state_code === 'human_endorsed') {
    return 'A person has already endorsed this, so there is nothing left to decide here. '
      + 'Re-verification runs through its own flow.'
  }
  if (item.kind === 'approved_join') {
    return 'The server is not offering you this confirmation: a discovered join needs two '
      + 'different admins, and an endorsement of yours is already recorded.'
  }
  const by = asStr(item.detail.proposed_by)
  return 'The server is not offering you this confirmation: the proposer of a fact cannot also '
    + `endorse it${by ? ` (proposed by ${by})` : ''}.`
}

// ── how the machine established the claim ────────────────────────────────────────────────────────
//
// `type_basis` changes WHAT a confirmation means, so it is rendered rather than carried silently.
// On the live catalogs every bridge is `declared`: two glossary SPREADSHEETS each answered with a
// type and the two answers agreed — nothing read the physical schema (bridge_candidates._resolve_
// family consults the declared type only when nothing was attested, and records the weaker basis).
// A reviewer ticking "these two columns identify the same customer" on that basis is endorsing two
// files, which is a materially different claim, so they see it before they tick.
const TYPE_BASIS: Record<string, { label: string; what: string; tone: string }> = {
  attested: {
    label: 'read from the data',
    what: 'Both sides were classified from the physical column type the catalog carries.',
    tone: 'ok',
  },
  declared: {
    label: 'declared in two spreadsheets',
    what: 'Nothing read the physical schema. The types matched because two glossary files each '
      + 'declared one and those two declarations agreed, so this rests on the files being right.',
    tone: 'warn',
  },
  mixed: {
    label: 'one side read, one side declared',
    what: 'One side was classified from the physical column type; the other only from what a '
      + 'glossary file declared.',
    tone: 'warn',
  },
}

// The same fact, said once more where the agreement is actually ticked — the one place a reviewer
// cannot be scrolled past it. Empty for a basis that carries no caveat.
function basisCaution(item: GovernanceQueueItem): string {
  if (item.kind !== 'entity_bridge') return ''
  const code = asStr(item.detail.type_basis)
  if (code === 'declared') {
    return 'Before you tick: the types here match because two glossary spreadsheets each said so. '
      + 'Nothing read the physical schema, so your agreement rests on those two files.'
  }
  if (code === 'mixed') {
    return 'Before you tick: only one of the two types was read from the data — the other is what '
      + 'a glossary file declared.'
  }
  if (!code) {
    return 'Before you tick: no derivation evidence was recorded for this crossing, so there is '
      + 'nothing on file about how the two types were matched.'
  }
  return ''
}

// A safe DOM id from a fact_key (which carries colons, dots and arrows).
function domId(prefix: string, factKey: string): string {
  return `${prefix}-${factKey.replaceAll(/[^a-zA-Z0-9_-]/g, '-')}`
}

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err)
}

// ── grouping the low-value cluster ───────────────────────────────────────────────────────────────

interface Entry {
  key: string
  // Non-null for a candidate GROUP: several bridges proposing the same entity between the same two
  // catalogs are a cross-product of the same two facts, not several findings.
  group: { entity: string; catalogs: string[] } | null
  items: GovernanceQueueItem[]
}

function entriesFor(kind: string, items: GovernanceQueueItem[]): Entry[] {
  if (kind !== 'entity_bridge') {
    return items.map(item => ({ key: item.fact_key, group: null, items: [item] }))
  }
  const buckets = new Map<string, GovernanceQueueItem[]>()
  for (const item of items) {
    const entity = asStr(item.detail.entity_id) || '(unnamed entity)'
    const key = `${entity}|${[...item.catalogs].sort().join('|')}`
    const bucket = buckets.get(key)
    if (bucket) bucket.push(item)
    else buckets.set(key, [item])
  }
  return [...buckets].map(([key, bucket]) => bucket.length > 1
    ? {
        key,
        group: {
          entity: asStr(bucket[0].detail.entity_id) || '(unnamed entity)',
          catalogs: bucket[0].catalogs,
        },
        items: bucket,
      }
    : { key: bucket[0].fact_key, group: null, items: bucket })
}

// ── small presentational pieces ──────────────────────────────────────────────────────────────────

function Axis({ testid, code, tone, label, value }: {
  testid: string
  code: string
  tone: string
  label: string
  value: string
}) {
  return (
    <div className="gq-axis" data-testid={testid} data-code={code} data-tone={tone}>
      <dt className="gq-axis-label">{label}</dt>
      <dd className={`gq-axis-value gq-tone-${tone}`}>{value}</dd>
    </div>
  )
}

// The three axes of one item, as three separate signals. Two come straight from the payload's code
// fields; the availability one is derived from `state_code` and omitted for a code it cannot map.
function Axes({ item }: { item: GovernanceQueueItem }) {
  const availability = AVAILABILITY[item.state_code]
  return (
    <dl className="gq-axes">
      {availability && (
        <Axis
          testid="axis-availability"
          code={item.state_code}
          tone={AVAILABILITY_TONE[item.state_code] ?? 'quiet'}
          label="Link availability"
          value={availability}
        />
      )}
      <Axis
        testid="axis-execution"
        code={item.production_eligibility_code}
        tone={EXECUTION_TONE[item.production_eligibility_code] ?? 'quiet'}
        label="Automatic execution safety"
        value={item.production_eligibility
          ?? EXECUTION_ABSENT[item.production_eligibility_code]
          ?? 'Not reported'}
      />
      <Axis
        testid="axis-review"
        code={item.state_code}
        tone={REVIEW_TONE[item.state_code] ?? 'quiet'}
        label="Human review"
        value={item.state}
      />
    </dl>
  )
}

// One "already depended on by" category. The three states render as three different things, and a
// number appears ONLY for a real measurement: `count` is null otherwise, so there is no path here
// that could turn "nothing was recorded" into 0.
function usageValue(usage: GovernanceQueueUsage): string {
  if (usage.state === 'counted' && usage.count !== null) return String(usage.count)
  if (usage.state === 'unreadable') return 'unreadable'
  return 'not tracked yet'
}

// The same measurement for a whole GROUP of bridges, category by category. A number is only ever
// reported when EVERY member of the group was counted: if one of them could not be measured, a sum
// across the rest would understate the consequence of settling the set, so the category falls back
// to the words. (In practice a category's state is uniform across one response — `usage_for_bridges`
// probes once per category and applies the verdict to every key — this is the defensive path.)
function groupUsages(items: GovernanceQueueItem[]): GovernanceQueueUsage[] {
  const order: string[] = []
  const byCategory = new Map<string, GovernanceQueueUsage[]>()
  for (const item of items) {
    for (const usage of item.already_depended_on_by) {
      const seen = byCategory.get(usage.category)
      if (seen) seen.push(usage)
      else {
        byCategory.set(usage.category, [usage])
        order.push(usage.category)
      }
    }
  }
  return order.map(category => {
    const all = byCategory.get(category) ?? []
    const counted = all.every(usage => usage.state === 'counted' && usage.count !== null)
    const state = all.some(usage => usage.state === 'unreadable')
      ? 'unreadable'
      : counted ? 'counted' : 'not_tracked_yet'
    const count = counted ? all.reduce((total, usage) => total + (usage.count ?? 0), 0) : null
    return { ...all[0], state, count, display: count === null ? state : String(count) }
  })
}

function Usage({ usages, note }: { usages: GovernanceQueueUsage[]; note?: string }) {
  // Bridges only. A join or table fact has no bridge anchor to count from, so there is nothing to
  // render — and an absent anchor is never "0 dependencies".
  if (usages.length === 0) return null
  return (
    <div className="gq-usage" data-testid="usage">
      <p className="gq-usage-head">Already depended on by</p>
      {note && <p className="gq-usage-note">{note}</p>}
      <dl className="gq-usage-list">
        {usages.map(usage => (
          <div className="gq-usage-item" key={usage.category} data-state={usage.state}>
            <dt className="gq-usage-cat">{categoryLabel(usage.category)}</dt>
            <dd
              className={`gq-usage-value gq-usage-${usage.state}`}
              data-testid={`usage-value-${usage.category}`}
            >
              {usageValue(usage)}
            </dd>
            <dd className="gq-usage-why">{usage.reason || usage.basis}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

// The derivation the reviewer is being asked to endorse: how the type match was established, which
// family matched, and where the link ranked. Bridges carry all three; every other kind carries none
// of them, and the block is ABSENT rather than rendered with blanks.
function Basis({ item }: { item: GovernanceQueueItem }) {
  const d = item.detail
  const code = asStr(d.type_basis)
  const family = asStr(d.data_type_family)
  const strength = typeof d.strength === 'number' ? d.strength : null
  // W4: `bridge_propose` skipped the ledger row, so there is no derivation evidence to describe —
  // said as an absence, never as a weak pass.
  const noEvidence = d.evidence_present === false
  if (!code && !family && strength === null && !noEvidence) return null
  const note = TYPE_BASIS[code]
  return (
    <div className="gq-basis" data-testid="gq-basis" data-type-basis={code || 'not_recorded'}>
      <p className="gq-basis-head">How this match was established</p>
      <dl className="gq-basis-list">
        <div
          className="gq-basis-item gq-basis-item--lead"
          data-testid="basis-type"
          data-tone={note?.tone ?? 'quiet'}
        >
          <dt className="gq-basis-label">Type match</dt>
          <dd className="gq-basis-value">
            {note?.label ?? (code ? code.replaceAll('_', ' ') : 'not recorded')}
          </dd>
          <dd className="gq-basis-why">
            {note?.what
              ?? (noEvidence || !code
                ? 'No derivation evidence was recorded for this crossing, so how the two types '
                  + 'were matched is not on file.'
                : 'This client cannot explain that basis, so it says nothing beyond the code.')}
          </dd>
        </div>
        <div className="gq-basis-item" data-testid="basis-family">
          <dt className="gq-basis-label">Matched type family</dt>
          <dd className="gq-basis-value">{family || 'not recorded'}</dd>
          <dd className="gq-basis-why">
            The two columns were paired because their types fall in the same family. Nothing here
            compared the values they hold.
          </dd>
        </div>
        <div className="gq-basis-item" data-testid="basis-strength">
          <dt className="gq-basis-label">Ranking strength</dt>
          <dd className="gq-basis-value">{strength === null ? 'not recorded' : String(strength)}</dd>
          <dd className="gq-basis-why">
            Where this link ranked against the other candidates for the same entity — it rewards a
            side that is its table key and a type read from the data. It is not a probability.
          </dd>
        </div>
      </dl>
    </div>
  )
}

// A dual join opens TWO side-labelled platform-admin tasks (`join_governance`), and which sides are
// still open is what tells a reviewer whether their endorsement finishes it. Joins only.
function Tasks({ item }: { item: GovernanceQueueItem }) {
  const tasks = (Array.isArray(item.detail.tasks) ? item.detail.tasks : []).map(asRec)
  if (tasks.length === 0) return null
  return (
    <div className="gq-side" data-testid="gq-tasks">
      <p className="gq-side-head">Approval tasks recorded for this join</p>
      <ul className="gq-side-list">
        {tasks.map((task, i) => (
          <li className="gq-side-item" key={asStr(task.task_id) || String(i)}>
            <span className="gq-side-key">{asStr(task.side) || 'unlabelled'} side</span>
            <span className="gq-side-val">{asStr(task.status) || 'status not reported'}</span>
            <span className="mono gq-side-id">{asStr(task.task_id)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// The table-level fields Pass B recorded alongside a grain / as-of proposal. DISPLAY-ONLY context
// (`table_fact_governance._ADVISORY_FIELDS`): it is not part of the claim being endorsed, and it
// says so, because an advisory field rendered like a finding would be read as one.
function Advisory({ item }: { item: GovernanceQueueItem }) {
  const fields = Object.entries(asRec(item.detail.advisory))
    .map(([key, value]): [string, string] => [key, asStr(value)])
    .filter(([, value]) => value !== '')
  if (fields.length === 0) return null
  return (
    <div className="gq-side" data-testid="gq-advisory">
      <p className="gq-side-head">What the enrichment said about this table</p>
      <ul className="gq-side-list">
        {fields.map(([key, value]) => (
          <li className="gq-side-item" key={key}>
            <span className="gq-side-key">{categoryLabel(key)}</span>
            <span className="gq-side-val">{value}</span>
          </li>
        ))}
      </ul>
      <p className="gq-side-note">
        Advisory context the enrichment offered. It is not part of what you are agreeing to, and
        nothing downstream reads it.
      </p>
    </div>
  )
}


function bridgeRealizations(item: GovernanceQueueItem): BridgeRealizationView[] {
  const value = item.detail.realizations
  if (!Array.isArray(value)) return []
  return value.filter(
    (entry): entry is BridgeRealizationView => (
      entry !== null
      && typeof entry === 'object'
      && typeof (entry as Record<string, unknown>).realization_id === 'string'
    ),
  )
}

function MetricSummary({ metric }: { metric: Record<string, unknown> }) {
  const fields: Array<[string, unknown]> = [
    ['Left rows', metric.left_row_count],
    ['Right rows', metric.right_row_count],
    ['Matched left IDs', metric.matched_left_distinct],
    ['Unmatched left IDs', metric.unmatched_left_distinct],
    ['Target duplicate rows', metric.right_duplicate_row_count],
    ['Maximum target matches per source row', metric.max_right_matches_per_left_row],
    ['Joined rows', metric.joined_row_count],
  ]
  return (
    <dl className="gq-usage-list" data-testid="realization-metrics">
      {fields.map(([label, value]) => (
        <div className="gq-usage-item" key={label}>
          <dt className="gq-usage-cat">{label}</dt>
          <dd className="gq-usage-value">{value === null || value === undefined
            ? 'Not observed'
            : String(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function RealizationEvidence({
  initial,
  onDone,
  onConflict,
}: {
  initial: BridgeRealizationView
  onDone: (message: string) => void
  onConflict: (detail: string) => void
}) {
  const [realization, setRealization] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function review(approved: boolean) {
    setBusy(true)
    setError('')
    try {
      const result = await reviewBridgeRealization(realization, approved)
      setRealization(result.realization)
      onDone(
        `${approved ? 'Endorsement recorded' : 'Endorsement removed'}. `
        + 'Automatic execution safety was not changed.',
      )
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        onConflict(e.detail)
      } else {
        setError(errorDetail(e))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="gq-usage" data-testid="realization-evidence">
      <p className="gq-usage-head">
        Directional realization: {realization.direction.from} → {realization.direction.to}
      </p>
      <dl className="gq-axes">
        <Axis
          testid="realization-cardinality"
          code={realization.cardinality}
          tone={realization.execution_eligible ? 'ok' : 'warn'}
          label="Join cardinality"
          value={realization.cardinality_label}
        />
        <Axis
          testid="realization-safety"
          code={realization.safety_status}
          tone={realization.execution_eligible ? 'ok' : 'warn'}
          label="Automatic safety"
          value={realization.execution_eligible
            ? 'Executable for the declared scope'
            : realization.execution_reason_codes.join(', ') || 'Not executable'}
        />
        <Axis
          testid="realization-review"
          code={realization.review_status}
          tone={realization.review_status === 'human_verified' ? 'ok' : 'quiet'}
          label="Optional human review"
          value={realization.review_status.replaceAll('_', ' ')}
        />
      </dl>
      <p className="gq-prov">
        Basis: {realization.cardinality_basis} · Lifecycle: {realization.lifecycle} · Evidence:{' '}
        {realization.evidence_fresh ? 'current' : 'not current'}
      </p>
      {realization.predicates.length > 0 && (
        <p className="gq-prov">
          Predicate scope: {realization.predicates
            .map(predicate => asStr(predicate.predicate_id) || asStr(predicate.kind))
            .join(', ')}
        </p>
      )}
      {realization.missing_requirements.length > 0 && (
        <p className="gq-error">
          Missing: {realization.missing_requirements
            .map(requirement => requirement.reason_code)
            .join(', ')}
        </p>
      )}
      {realization.metrics.map((metric, index) => (
        <MetricSummary metric={metric} key={asStr(metric.observation_revision_id) || index} />
      ))}
      {realization.profile_action && (
        <p className="gq-prov">
          Next action: {realization.profile_action.label} ({realization.profile_action.state})
        </p>
      )}
      <div className="gj-actions gq-actions">
        <button type="button" className="btn" disabled={busy} onClick={() => void review(true)}>
          Endorse realization
        </button>
        <button type="button" className="btn" disabled={busy} onClick={() => void review(false)}>
          Remove endorsement
        </button>
      </div>
      {error && <p className="gq-error" role="alert">{error}</p>}
    </section>
  )
}

function BridgeEvidence({
  item,
  onDone,
  onConflict,
}: {
  item: GovernanceQueueItem
  onDone: (message: string) => void
  onConflict: (detail: string) => void
}) {
  if (item.kind !== 'entity_bridge') return null
  const assessment = asRec(item.detail.assessment)
  const left = asRec(assessment.left_endpoint)
  const right = asRec(assessment.right_endpoint)
  const authority = asRec(item.detail.authority)
  const hypothesis = asStr(assessment.population_hypothesis)
  const contradiction = asStr(assessment.strongest_contradiction)
  const reasons = asStrArr(assessment.proposal_reasons)
  const realizations = bridgeRealizations(item)
  return (
    <section className="gq-bridge-evidence" data-testid="bridge-evidence">
      <dl className="gq-usage-list">
        <div className="gq-usage-item">
          <dt className="gq-usage-cat">Join cardinality</dt>
          <dd className="gq-usage-value">
            {asStr(item.detail.cardinality_label) || 'Not evaluated'}
          </dd>
        </div>
        <div className="gq-usage-item">
          <dt className="gq-usage-cat">Namespace verdict</dt>
          <dd className="gq-usage-value">
            {asStr(assessment.namespace_verdict) || 'Not evaluated'}
          </dd>
        </div>
        <div className="gq-usage-item">
          <dt className="gq-usage-cat">Governed population relation</dt>
          <dd className="gq-usage-value">
            {asStr(assessment.governed_population_relation) || 'Unknown'}
          </dd>
        </div>
        <div className="gq-usage-item">
          <dt className="gq-usage-cat">Concept authority</dt>
          <dd className="gq-usage-value">
            {[asStr(left.concept_authority), asStr(right.concept_authority)]
              .filter(Boolean).join(' / ') || 'Unknown'}
          </dd>
        </div>
        <div className="gq-usage-item">
          <dt className="gq-usage-cat">Human review authority</dt>
          <dd className="gq-usage-value">
            {asStr(authority.role)
              ? `${asStr(authority.role)} · ${String(authority.confirmation_count ?? 1)} confirmer`
              : 'Not reported'}
          </dd>
        </div>
      </dl>
      {hypothesis && <p className="gq-prov">Advisory population hypothesis: {hypothesis}</p>}
      {reasons.length > 0 && <p className="gq-prov">Proposed because: {reasons.join(', ')}</p>}
      {contradiction && (
        <p className="gq-error">Strongest contradiction: {contradiction}</p>
      )}
      {realizations.map(realization => (
        <RealizationEvidence
          initial={realization}
          key={realization.realization_id}
          onDone={onDone}
          onConflict={onConflict}
        />
      ))}
      {realizations.length === 0 && (
        <p className="gq-prov">
          No directional realization has been evaluated. Run a bounded profile in the data
          environment; reviewing the identifier link will not run it.
        </p>
      )}
    </section>
  )
}

function CategoryChips({ kind, chosen, onPick, label }: {
  kind: string
  chosen: string | null
  onPick: (category: string) => void
  label: string
}) {
  return (
    <div className="gj-chips" role="group" aria-label={label}>
      {rejectCategories(kind).map(category => (
        <button
          type="button"
          key={category}
          className={category === chosen ? 'gj-chip gj-chip--on' : 'gj-chip'}
          aria-pressed={category === chosen}
          onClick={() => onPick(category)}
        >
          {categoryLabel(category)}
        </button>
      ))}
    </div>
  )
}

// ── the command layer, dispatched by kind ────────────────────────────────────────────────────────

interface Outcome {
  governance_status: string
  projection: string
  projectionKind: 'review' | 'operational'
}

async function confirmItem(item: GovernanceQueueItem, note: string): Promise<Outcome> {
  const body = note ? { note } : {}
  if (item.kind === 'entity_bridge') {
    const result = await confirmEntityBridge(item.fact_key, body)
    return {
      governance_status: result.governance_status,
      projection: result.review_projection,
      projectionKind: 'review',
    }
  }
  if (item.kind === 'currency_binding' || item.kind === 'entity_assignment') {
    const result = await confirmSemanticBinding(item.fact_key, body)
    return {
      governance_status: result.governance_status,
      projection: result.operational_projection,
      projectionKind: 'operational',
    }
  }
  if (item.kind === 'approved_join') {
    const result = await confirmJoin(item.fact_key, body)
    return {
      governance_status: result.governance_status,
      projection: result.operational_projection,
      projectionKind: 'operational',
    }
  }
  if (item.kind === 'grain' || item.kind === 'availability_time') {
    const result = await confirmTableFact(item.fact_key, body)
    return {
      governance_status: result.governance_status,
      projection: result.operational_projection,
      projectionKind: 'operational',
    }
  }
  // VERSION SKEW, named (2026-08-10): an old bundle once met a new queue kind here and fell
  // through to the TABLE-FACT command — the backend 404ed the wrong-route confirm ("No such
  // table-fact proposal"), which read as a broken screen. A kind this build does not know gets a
  // reload instruction, never somebody else's command.
  throw new Error(
    `This review screen is older than this decision kind (${item.kind}). Reload the page.`)
}

async function rejectItem(
  item: GovernanceQueueItem,
  category: string,
  note: string,
): Promise<void> {
  const rest = note ? { note } : {}
  // The category came from this kind's OWN vocabulary list, which is why the narrowing is safe.
  if (item.kind === 'entity_bridge') {
    await rejectEntityBridge(item.fact_key,
      { category: category as EntityBridgeRejectCategory, ...rest })
    return
  }
  if (item.kind === 'approved_join') {
    await rejectJoin(item.fact_key, { category: category as RejectCategory, ...rest })
    return
  }
  if (item.kind === 'currency_binding' || item.kind === 'entity_assignment') {
    await rejectSemanticBinding(item.fact_key,
      { category: category as SemanticBindingRejectCategory, ...rest })
    return
  }
  if (item.kind === 'grain' || item.kind === 'availability_time') {
    await rejectTableFact(item.fact_key,
      { category: category as TableFactRejectCategory, ...rest })
    return
  }
  throw new Error(
    `This review screen is older than this decision kind (${item.kind}). Reload the page.`)
}

function projectionNote(projection: string, kind: Outcome['projectionKind']): string {
  if (kind === 'review' && projection === 'projected') {
    return ' The review projection was updated; availability and execution safety did not change.'
  }
  if (kind === 'review' && projection === 'pending') {
    return ' The endorsement was recorded; its review projection is pending.'
  }
  if (projection === 'projected') return ' The operational projection is live and stays revocable.'
  if (projection === 'pending') {
    return ' The operational projection is deferred to the next caught-up ingest.'
  }
  if (projection === 'demoted') return ' Any operational link it had was removed.'
  return ''
}

// ── one row ──────────────────────────────────────────────────────────────────────────────────────

interface RowProps {
  item: GovernanceQueueItem
  onDone: (message: string) => void
  onConflict: (detail: string) => void
}

function QueueRow({ item, onDone, onConflict }: RowProps) {
  const [panel, setPanel] = useState<'none' | 'confirm' | 'reject'>('none')
  const [agreed, setAgreed] = useState(false)
  const [note, setNote] = useState('')
  const [category, setCategory] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [rowError, setRowError] = useState('')

  const canConfirm = item.available_actions.includes('confirm')
  const canReject = item.available_actions.includes('reject')
  const whyId = domId('why', item.fact_key)
  const provenance = provenanceParts(item)
  const caution = basisCaution(item)

  async function run(action: () => Promise<string>) {
    setBusy(true)
    setRowError('')
    try {
      onDone(await action())
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // The fact moved under the reviewer (a concurrent decision, a stale CAS target). Never
        // blind-retry: surface the server's sentence and let the reload bring the fresh row.
        onConflict(e.detail)
        return
      }
      setRowError(errorDetail(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gq-row" data-testid={`row-${item.fact_key}`}>
      <div className="gq-row-head">
        <span className="gq-row-kind">{kindLabelOne(item.kind)}</span>
        {item.catalogs.map(slug => (
          <span className="badge gq-catalog" key={slug} data-testid="gq-catalog">
            {catalogLabel(slug)}
          </span>
        ))}
      </div>
      <p className="gq-row-headline">{headline(item)}</p>
      <p className="mono gq-row-subject">{item.subject}</p>
      <Axes item={item} />
      <Basis item={item} />
      <Tasks item={item} />
      <Advisory item={item} />
      {provenance.length > 0 && <p className="gq-prov">{provenance.join(' · ')}</p>}
      <Usage usages={item.already_depended_on_by} />
      <BridgeEvidence item={item} onDone={onDone} onConflict={onConflict} />

      <div className="gj-actions gq-actions">
        <button
          type="button"
          className="btn btn--primary"
          disabled={!canConfirm || busy}
          aria-describedby={canConfirm ? undefined : whyId}
          onClick={() => setPanel(p => (p === 'confirm' ? 'none' : 'confirm'))}
        >
          {panel === 'confirm' ? 'Cancel' : 'Confirm…'}
        </button>
        {canReject && (
          <button
            type="button"
            className="btn q-ghost"
            disabled={busy}
            onClick={() => setPanel(p => (p === 'reject' ? 'none' : 'reject'))}
          >
            {panel === 'reject' ? 'Cancel reject' : 'Reject…'}
          </button>
        )}
        {!canConfirm && (
          <span className="gq-why" id={whyId} data-testid="gq-action-why">
            {withheldReason(item)}
          </span>
        )}
      </div>

      {panel === 'confirm' && (
        <div className="gq-panel">
          <p className="gq-panel-head">What your confirmation records</p>
          {caution && (
            <p className="gq-panel-caution" data-testid="gq-confirm-basis">{caution}</p>
          )}
          <label className="gj-check">
            <input type="checkbox" checked={agreed} onChange={() => setAgreed(a => !a)} />
            <span>{agreement(item)}</span>
          </label>
          <p className="gq-panel-note">
            This records that a person agrees with the relationship, and who. It does not change
            whether the platform may use it, and it does not change the automatic execution-safety
            verdict above — both of those are decided without you.
          </p>
          <input
            aria-label="Note for the record (optional)"
            placeholder="Optional note — what you checked; recorded for audit"
            value={note}
            onChange={e => setNote(e.target.value)}
          />
          <div className="gj-actions">
            <button
              type="button"
              className="btn btn--primary"
              disabled={!agreed || busy}
              onClick={() => void run(async () => {
                const result = await confirmItem(item, note.trim())
                return `Your agreement is recorded — ${headline(item)} is now `
                  + `${result.governance_status.toLowerCase().replaceAll('_', ' ')}.`
                  + projectionNote(result.projection, result.projectionKind)
              })}
            >
              {busy ? 'Recording…' : 'Record my confirmation'}
            </button>
            {!agreed && <span className="gj-gate-hint">tick the statement you agree with</span>}
          </div>
        </div>
      )}

      {panel === 'reject' && (
        <div className="gq-panel">
          <p className="gq-panel-head">Why this is not a real relationship</p>
          <CategoryChips
            kind={item.kind}
            chosen={category}
            onPick={setCategory}
            label="Rejection reason"
          />
          <input
            aria-label="Rejection note (optional)"
            placeholder="Optional note…"
            value={note}
            onChange={e => setNote(e.target.value)}
          />
          <div className="gj-actions">
            <button
              type="button"
              className="btn btn--danger"
              disabled={!category || busy}
              onClick={() => void run(async () => {
                const chosen = category ?? ''
                await rejectItem(item, chosen, note.trim())
                return `Recorded as ${categoryLabel(chosen)} — ${headline(item)} will not be `
                  + 'treated as a real relationship. Your reason is on the governance dashboard.'
              })}
            >
              {busy ? 'Recording…' : 'Record my rejection'}
            </button>
            {!category && <span className="gj-gate-hint">pick a reason first</span>}
          </div>
        </div>
      )}

      {rowError && (
        <p className="field-error" role="alert">
          {rowError}
        </p>
      )}
    </div>
  )
}

// ── one candidate group ──────────────────────────────────────────────────────────────────────────

// Several bridges proposing the SAME entity between the SAME two catalogs are the cross-product of
// two facts, not several findings, so they get one card and one judgement. Reject-all fans out to
// one governed command per key server-side (each with its own audit row and its own savepoint),
// which is why partial outcomes are ordinary and the result is reported as a split.
function CandidateGroup({ entry, onDone, onConflict }: {
  entry: Entry
  onDone: (message: string) => void
  onConflict: (detail: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [category, setCategory] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [groupError, setGroupError] = useState('')

  const items = entry.items
  const group = entry.group ?? { entity: '(unnamed entity)', catalogs: [] }
  const count = items.length
  // THE SERVER DECIDES HERE TOO. A bridge already endorsed carries no actions at all
  // (`_ACTIONS_VERIFIED = ()`) and the write side denies rejecting a VERIFIED fact, so a group
  // whose members are a mix — the ordinary end state after confirming one member of a
  // cross-product — must offer the reject only over the members that sanction it, and say what it
  // is leaving alone. The individual rows inside "Show the N candidates" already read their own
  // `available_actions`; the card that stands in for them may not read something else.
  const rejectable = items.filter(item => item.available_actions.includes('reject'))
  const skipped = count - rejectable.length
  const one = skipped === 1
  const skipNote = ` The ${skipped} already endorsed ${one ? 'link is' : 'links are'} not sent at `
    + `all: the command layer refuses a rejection there, so nothing is attempted on `
    + `${one ? 'it' : 'them'}.`

  // The two axes again at group level: one distinct value each, or an honest "mixed".
  function shared(read: (item: GovernanceQueueItem) => string): string {
    const values = new Set(items.map(read))
    return values.size === 1 ? [...values][0] : `mixed across the ${count} candidates`
  }

  async function rejectAll() {
    if (!category || rejectable.length === 0) return
    setBusy(true)
    setGroupError('')
    try {
      const result = await bulkRejectEntityBridges(
        rejectable.map(item => item.fact_key),
        category as EntityBridgeRejectCategory,
        note.trim() || undefined,
      )
      const settled = (result.counts.rejected ?? 0) + (result.counts.already_rejected ?? 0)
      const refused = (result.counts.denied ?? 0) + (result.counts.not_found ?? 0)
        + (result.counts.failed ?? 0)
      onDone(`${settled} of ${rejectable.length} candidate links recorded as `
        + `${categoryLabel(category)}`
        + `${refused > 0 ? `; ${refused} the server did not settle` : ''}`
        + `${skipped > 0 ? `; ${skipped} already endorsed and left as they are` : ''}.`)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        onConflict(e.detail)
        return
      }
      setGroupError(errorDetail(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gq-group">
      <div className="gq-row-head">
        <span className="gq-row-kind">{kindLabelOne('entity_bridge')}s</span>
        {group.catalogs.map(slug => (
          <span className="badge gq-catalog" key={slug} data-testid="gq-catalog">
            {catalogLabel(slug)}
          </span>
        ))}
      </div>
      <p className="gq-row-headline">
        {count} candidate links for the same {group.entity}
        {group.catalogs.length === 2
          ? `, between ${group.catalogs.map(catalogLabel).join(' and ')}`
          : ''}
      </p>
      <p className="gq-group-note">
        These are the cross-product of the same two facts — one judgement settles the set. Open them
        if one of the pairs is the right one.
      </p>
      {skipped > 0 && (
        <p className="gq-group-note" data-testid="gq-group-settled">
          {skipped} of these {count} {one ? 'is' : 'are'} already endorsed. The server offers no
          rejection on {one ? 'it' : 'them'}, so a group rejection here settles the other{' '}
          {rejectable.length} and leaves {one ? 'that one as it is' : 'those as they are'}.
        </p>
      )}
      <dl className="gq-axes">
        <Axis
          testid="axis-execution"
          code="group"
          tone="quiet"
          label="Automatic execution safety"
          value={shared(item => item.production_eligibility
            ?? EXECUTION_ABSENT[item.production_eligibility_code] ?? 'Not reported')}
        />
        <Axis
          testid="axis-review"
          code="group"
          tone="quiet"
          label="Human review"
          value={shared(item => item.state)}
        />
      </dl>

      <Usage
        usages={groupUsages(items)}
        note={`Counted across all ${count} links in this group. A dependent that crosses more than `
          + 'one of them is counted once per link, and a category is only ever a number when every '
          + 'one of the links was measured.'}
      />

      <div className="gj-actions gq-actions">
        {rejectable.length > 0 && (
          <button
            type="button"
            className="btn q-ghost"
            disabled={busy}
            onClick={() => setRejecting(r => !r)}
          >
            {rejecting
              ? 'Cancel'
              : skipped === 0
                ? 'Reject the whole group…'
                : `Reject the ${rejectable.length} still open…`}
          </button>
        )}
        {rejectable.length === 0 && (
          <span className="gq-why" data-testid="gq-group-why">
            A person has already endorsed every one of these {count} links, so the server offers no
            decision to make here. Re-verification runs through its own flow.
          </span>
        )}
        <button type="button" className="btn q-ghost" onClick={() => setOpen(o => !o)}>
          {open ? `Hide the ${count} candidates` : `Show the ${count} candidates`}
        </button>
      </div>

      {rejecting && (
        <div className="gq-panel">
          <p className="gq-panel-head">
            Why none of these {rejectable.length} pairs is a real relationship
          </p>
          <CategoryChips
            kind="entity_bridge"
            chosen={category}
            onPick={setCategory}
            label="Rejection reason for the group"
          />
          <input
            aria-label="Group rejection note (optional)"
            placeholder="Optional note…"
            value={note}
            onChange={e => setNote(e.target.value)}
          />
          <div className="gj-actions">
            <button
              type="button"
              className="btn btn--danger"
              disabled={!category || busy}
              onClick={() => void rejectAll()}
            >
              {busy
                ? 'Recording…'
                : skipped === 0
                  ? `Reject all ${count} candidates`
                  : `Reject the ${rejectable.length} still open`}
            </button>
            {!category && <span className="gj-gate-hint">pick a reason first</span>}
          </div>
          <p className="gq-panel-note">
            Each one is recorded as its own governed decision with its own audit trail, so some may
            settle while others are refused — the result says which.
            {skipped > 0 && skipNote}
          </p>
        </div>
      )}

      {open && (
        <div role="group" aria-label={`The ${count} candidate links in this group`}>
          <ul className="rows gq-members">
            {items.map(item => (
              <li className="row q-item gq-member" key={item.fact_key}>
                <QueueRow item={item} onDone={onDone} onConflict={onConflict} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {groupError && (
        <p className="field-error" role="alert">
          {groupError}
        </p>
      )}
    </div>
  )
}

// The page's own reason for existing, stated before any row: these are the judgements a machine
// cannot make on its own behalf.
function Purpose(): ReactNode {
  return (
    <div className="callout callout--accent gq-purpose" data-testid="gq-purpose">
      <div className="callout-body">
        <p>
          <strong>Decisions only a person can make.</strong> The system proposes and you dispose:
          everything below was derived automatically, and what it needs from you is a judgement
          about meaning. Confirming records that a human agrees with a relationship — who agreed,
          and when. It is not a switch, and nothing here is held back pending your say-so.
        </p>
      </div>
    </div>
  )
}

// ── the screen ───────────────────────────────────────────────────────────────────────────────────

// initialSource: the governance dashboard -> review handoff rides the URL (?source=). It is a
// PRESELECTED FILTER now, never a precondition — the queue loads whole either way, and a slug that
// names no visible catalog is dropped rather than left filtering everything out.
export function GovernanceReviewScreen({ initialSource = '' }: { initialSource?: string }) {
  const [queue, setQueue] = useState<GovernanceQueue | null>(null)
  const [error, setError] = useState('')
  // The status the FAILURE carried, so a refusal and a breakage can be told apart. There is no
  // client-side permission check to make here — the session's roles are not on this client — so the
  // screen reacts to what the server said rather than predicting it.
  const [errorStatus, setErrorStatus] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState('')
  const [limit, setLimit] = useState(100)
  const [catalog, setCatalog] = useState<string | null>(initialSource.trim().toLowerCase() || null)
  const [kindFilter, setKindFilter] = useState<string | null>(null)

  // Monotonic id per load: a late response from a superseded load must never overwrite newer data.
  const loadSeq = useRef(0)

  const load = useCallback(async () => {
    const id = ++loadSeq.current
    setLoading(true)
    try {
      const next = await getGovernanceQueue(limit)
      if (id !== loadSeq.current) return
      setQueue(next)
      setError('')
      setErrorStatus(null)
      // A handoff slug for a catalog this caller cannot see would otherwise filter the screen down
      // to nothing — exactly the blank page this rewrite exists to remove.
      setCatalog(current => (current !== null && !next.catalogs.includes(current) ? null : current))
    } catch (e) {
      if (id !== loadSeq.current) return
      setQueue(null)
      setError(errorDetail(e))
      setErrorStatus(e instanceof ApiError ? e.status : null)
    } finally {
      if (id === loadSeq.current) setLoading(false)
    }
  }, [limit])

  // The whole point: the work is fetched on arrival. No source to type, no button to press.
  useEffect(() => {
    void load()
  }, [load])

  // A later deep link can change the handoff source while the screen stays mounted.
  useEffect(() => {
    setCatalog(initialSource.trim().toLowerCase() || null)
  }, [initialSource])

  function onDone(message: string) {
    setNotice(message)
    void load()
  }

  function onConflict(detail: string) {
    setNotice(detail)
    void load()
  }

  // NOT AN ERROR. `GET /governance/queue` is gated on the raw `platform-admin` claim, and nothing
  // in the app gates the navigation to it — the "Governance" item is on every operator's nav, and
  // LineageView links here in prose — so a catalog_viewer, data_owner or feature_engineer arriving
  // here is an ordinary, expected visit that the server declines. A red alert would tell them
  // something is broken and invite them to retry; nothing is broken and there is nothing to retry.
  //
  // The screen cannot check the role itself (this client holds no session claims, and inventing a
  // check would be a second, drifting authority), so it reacts to the 403 the server actually sent
  // and quotes the server's own sentence rather than paraphrasing the rule.
  if (errorStatus === 403) {
    return (
      <section className="gq">
        <Purpose />
        <div className="callout gq-not-yours" data-testid="gq-not-yours" role="status">
          <div className="callout-body">
            <p>
              <strong>This queue is not open to your role.</strong> Recording a governance decision
              is a platform-administrator act, so the server declined to show this list to your
              session — it said: “{error}”.
            </p>
            <p>
              Nothing is wrong and nothing here is waiting on you. The catalogs, features and
              lineage you can see are unaffected: what is behind this page is the record of who
              agreed with a relationship, not a control over what the platform will use.
            </p>
            <p className="hint">
              If reviewing these decisions is part of your job, ask an administrator for the
              platform-admin role — this page will then open on the queue itself.
            </p>
          </div>
        </div>
      </section>
    )
  }

  if (error) {
    return (
      <section className="gq">
        <Purpose />
        <p role="alert" className="error">
          {error}
        </p>
      </section>
    )
  }

  if (!queue) {
    return (
      <section className="gq">
        <Purpose />
        <p className="empty" role="status">
          {loading ? 'Loading every decision waiting for you…' : 'Nothing loaded.'}
        </p>
      </section>
    )
  }

  const items = queue.items.filter(item =>
    (catalog === null || item.catalogs.includes(catalog))
    && (kindFilter === null || item.kind === kindFilter))
  // The kind list comes from the payload (order included), never from a hardcoded set — an unknown
  // kind from a newer backend gets a chip, a section and a readable label.
  const kinds = Object.keys(queue.items_visible_to_you_by_kind)
  const shownKinds = kindFilter === null ? kinds : kinds.filter(kind => kind === kindFilter)
  // WHAT ACTUALLY NEEDS A PERSON is not the list length: the bridge listing also carries VERIFIED
  // facts, and those offer no action at all (`reject_fact` denies them). Counting them as waiting
  // would overstate the work, so the headline counts only rows the server still offers an action on.
  const waiting = items.filter(item => item.available_actions.length > 0).length
  const yours = items.filter(item => item.available_actions.includes('confirm')).length
  const elsewhere = items.filter(item =>
    item.available_actions.length > 0 && !item.available_actions.includes('confirm')).length
  const endorsed = items.filter(item => item.available_actions.length === 0).length

  return (
    <section className="gq">
      <Purpose />

      {notice && (
        <p role="status" className="callout callout--accent gq-notice" data-testid="gq-notice">
          {notice}
        </p>
      )}

      {!queue.complete && (
        <div className="callout callout--warn" data-testid="gq-incomplete" role="status">
          <div className="callout-body">
            <p>
              <strong>This list is incomplete.</strong> Something could not be read, so what is
              below is what we could see — not necessarily everything that is waiting.
            </p>
            <ul>
              {queue.unreadable.map(entry => (
                <li key={`${entry.listing}:${entry.source ?? ''}`}>
                  {kindLabel(entry.listing)}
                  {entry.source ? ` in ${catalogLabel(entry.source)}` : ''}: {entry.reason}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div className="stats gq-summary" data-testid="gq-summary">
        <div className="stat">
          <b>{waiting}</b> waiting for a person
        </div>
        <div className="stat">
          <b>{yours}</b> you can decide
        </div>
        <div className="stat">
          <b>{elsewhere}</b> need a different reviewer
        </div>
        {endorsed > 0 && (
          <div className="stat">
            <b>{endorsed}</b> already endorsed
          </div>
        )}
        {shownKinds.map(kind => (
          <div className="stat" key={kind}>
            <b>{items.filter(item => item.kind === kind).length}</b> {kindLabel(kind).toLowerCase()}
          </div>
        ))}
      </div>
      <p className="hint" data-testid="gq-scope-note">
        Across {queue.catalogs.length} catalog{queue.catalogs.length === 1 ? '' : 's'} you can see
        {queue.catalogs.length > 0 ? `: ${queue.catalogs.map(catalogLabel).join(', ')}` : ''}. Every
        count here is scope-relative — it describes what you can see, never a catalog total.
      </p>

      <div className="gq-filters">
        <div className="gj-chips" role="group" aria-label="Catalog" data-testid="gq-catalog-filter">
          <button
            type="button"
            className={catalog === null ? 'gj-chip gj-chip--on' : 'gj-chip'}
            aria-pressed={catalog === null}
            onClick={() => setCatalog(null)}
          >
            All catalogs
          </button>
          {queue.catalogs.map(slug => (
            <button
              type="button"
              key={slug}
              className={catalog === slug ? 'gj-chip gj-chip--on' : 'gj-chip'}
              aria-pressed={catalog === slug}
              onClick={() => setCatalog(slug)}
            >
              {catalogLabel(slug)} ({queue.items_visible_to_you_by_catalog[slug] ?? 0})
            </button>
          ))}
        </div>
        <div
          className="gj-chips"
          role="group"
          aria-label="Decision kind"
          data-testid="gq-kind-filter"
        >
          <button
            type="button"
            className={kindFilter === null ? 'gj-chip gj-chip--on' : 'gj-chip'}
            aria-pressed={kindFilter === null}
            onClick={() => setKindFilter(null)}
          >
            All kinds
          </button>
          {kinds.map(kind => (
            <button
              type="button"
              key={kind}
              className={kindFilter === kind ? 'gj-chip gj-chip--on' : 'gj-chip'}
              aria-pressed={kindFilter === kind}
              onClick={() => setKindFilter(kind)}
            >
              {kindLabel(kind)} ({queue.items_visible_to_you_by_kind[kind] ?? 0})
            </button>
          ))}
        </div>
      </div>

      {shownKinds.map(kind => {
        const forKind = items.filter(item => item.kind === kind)
        const entries = entriesFor(kind, forKind)
        const broken = queue.unreadable.filter(entry =>
          unreadableListings(kind).includes(entry.listing))
        const where = catalog === null ? 'the catalogs you can see' : catalogLabel(catalog)
        return (
          <section className="gq-kind" key={kind} data-testid={`kind-${kind}`}>
            <h3 className="gq-kind-head">
              {kindLabel(kind)} <span className="tabular-nums">{forKind.length}</span>
            </h3>
            {KIND_ABOUT[kind] && <p className="hint gq-kind-about">{KIND_ABOUT[kind]}</p>}
            {forKind.length === 0 && broken.length > 0 && (
              <p className="empty gq-broken">
                We could not look here.{' '}
                {broken.map(entry => `${entry.reason}${entry.source
                  ? ` (${catalogLabel(entry.source)})`
                  : ''}`).join('; ')}. An empty list here is not a settled one.
              </p>
            )}
            {forKind.length === 0 && broken.length === 0 && (
              <p className="empty gq-settled" role="status">
                Nothing to review — no {kindLabelOne(kind).toLowerCase()} is waiting for a decision
                in {where}. Everything proposed here has already been decided, and the platform
                keeps working either way.
              </p>
            )}
            {entries.length > 0 && (
              <ul className="rows">
                {entries.map(entry => (
                  <li className="row q-item" key={entry.key} data-testid="queue-entry">
                    {entry.group
                      ? <CandidateGroup entry={entry} onDone={onDone} onConflict={onConflict} />
                      : (
                          <QueueRow
                            item={entry.items[0]}
                            onDone={onDone}
                            onConflict={onConflict}
                          />
                        )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        )
      })}

      {queue.truncated && (
        <p className="hint">
          More decisions are waiting than this page holds (it shows up to {limit}).{' '}
          <button type="button" className="btn q-ghost" onClick={() => setLimit(500)}>
            Show more
          </button>
        </p>
      )}

      {/* Data-use policies (D14). A STANDING decision rather than a queued one — nothing proposes
          it, so it has no queue row and would have no home if it were not here. It belongs on the
          DECIDING screen rather than the read-only dashboard: the feature flow's refusal sends the
          reviewer to "Governance -> Data-use policies", and this is where deciding happens. */}
      <ConceptConfirmationPanel />
      <DataUsePolicyPanel />
    </section>
  )
}
