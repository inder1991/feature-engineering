import { useCallback, useEffect, useRef, useState } from 'react'
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
import { varyingParts } from './candidateDiff'

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
// A lead-in earns its place ONLY by stating a consequence the rows cannot state for themselves.
// The definitional ones are gone: "The same business entity in two different catalogs" sat
// directly above a card reading "2 candidate links for the same branch, between CIB and FTR" —
// the abstraction printed above its own worked example — and "these are the decisions no
// per-catalog screen could show you" explained the system's architecture to someone who only
// wants to know what they are approving. What survives here says something new.
const KIND_ABOUT: Record<string, string> = {
  // NOT symmetric: `list_open_approved_join_proposals` filters on from_ref.catalog_source only, so
  // which catalog a join is filed under is a real surprise the rows never mention.
  approved_join: 'A discovered join is listed under the catalog it starts in, not under both.',
  // A refusal elsewhere in the product, caused by this decision. Nothing on the card says it.
  currency_binding: 'Features that sum money are refused until this is decided.',
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
        items: byStrength(bucket),
      }
    : { key: bucket[0].fact_key, group: null, items: bucket })
}

// WHAT YOU CAN DECIDE, FIRST. Payload order is insertion order, so endorsed facts and rows waiting
// on somebody else sat interleaved with the reviewer's own work: the summary said "12 you can
// decide" and the list gave no clue where they were. Three tiers — yours, someone else's, settled —
// and the sort is stable, so within a tier the strongest-first order set by `byStrength` survives.
// This orders ENTRIES, never the members inside a group: those are ranked by evidence, and
// reordering them by what the server will let you press would hide the strongest candidate.
function byUrgency(entries: Entry[]): Entry[] {
  const tier = (entry: Entry): number => {
    if (entry.items.some(item => item.available_actions.includes('confirm'))) return 0
    return entry.items.some(item => item.available_actions.length > 0) ? 1 : 2
  }
  return [...entries].sort((a, b) => tier(a) - tier(b))
}

// Strongest first. Payload order is insertion order and carries no judgement, so a reviewer
// scanning a cross-product top-down would meet the candidates in an order the ranker did not
// choose. Sorted ONCE here so the comparison table and the dossiers behind the disclosure can
// never disagree about which candidate is first. A missing strength sorts last rather than as a
// zero: "not recorded" is not a low score. The sort is stable, so ties keep payload order.
function byStrength(items: GovernanceQueueItem[]): GovernanceQueueItem[] {
  const rank = (item: GovernanceQueueItem): number =>
    (typeof item.detail.strength === 'number' ? item.detail.strength : -Infinity)
  return [...items].sort((a, b) => rank(b) - rank(a))
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
  // NOT ONE MEASUREMENT ANYWHERE — so there is nothing to render. This block once listed five
  // "not tracked yet" cells with a paragraph of store-level rationale on each, repeated on the
  // group card AND every member: 85 times across the page. Collapsing that to one sentence was
  // still the wrong shape, because the sentence said "this says nothing about what uses it" — text
  // that disclaims its own informational value, which an absent block does in no words. On the
  // live catalogs none of the five stores records a bridge dependency at all, so this was a
  // permanent placeholder for a capability that does not exist. The moment any category returns a
  // real measurement the block is back, with a number in it.
  const measured = usages.some(usage => usage.state === 'counted' && usage.count !== null)
  const unreadable = usages.some(usage => usage.state === 'unreadable')
  if (!measured && !unreadable) return null

  return (
    <div className="gq-usage" data-testid="usage">
      <p className="gq-usage-head">Already depended on by</p>
      {note && <p className="gq-usage-note">{note}</p>}
      <UsageList usages={usages} />
    </div>
  )
}

function UsageList({ usages }: { usages: GovernanceQueueUsage[] }) {
  return (
    <>
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
    </>
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
  // The fact_key is passed back so the screen can land the outcome on the row that produced it.
  onDone: (message: string, factKey?: string) => void
  onConflict: (detail: string) => void
  // The message this row's own last decision produced, if the fact is still in the queue.
  outcome?: string
  // Which panel the row opens on. A candidate reached from the comparison table opens straight
  // onto its confirmation — the reviewer already chose it there, so making them press Confirm a
  // second time to reach the same gate is a step that decides nothing.
  initialPanel?: 'none' | 'confirm' | 'reject'
}

function QueueRow({ item, onDone, onConflict, initialPanel = 'none', outcome }: RowProps) {
  const [panel, setPanel] = useState<'none' | 'confirm' | 'reject'>(initialPanel)
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
      onDone(await action(), item.fact_key)
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
      <BridgeEvidence
        item={item}
        onDone={message => onDone(message, item.fact_key)}
        onConflict={onConflict}
      />

      {outcome && (
        <p className="gq-row-outcome" data-testid="gq-row-outcome" role="status">
          {outcome}
        </p>
      )}

      {/* An action the server does not offer is not an action. It used to render as a DIMMED
          PRIMARY button — the loudest control on the card, at 55% opacity, doing nothing — and on
          the live catalogs every currency binding withholds confirm, so the three most prominent
          controls on the page were all dead. The row now shows what the reviewer can do, and says
          why the rest is not there. */}
      <div className="gj-actions gq-actions">
        {canConfirm && (
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            onClick={() => setPanel(p => (p === 'confirm' ? 'none' : 'confirm'))}
          >
            {panel === 'confirm' ? 'Cancel' : 'Confirm…'}
          </button>
        )}
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

// The group as a COMPARISON, which is what the reviewer is actually being asked for: the members
// of a cross-product differ in one place and agree everywhere else, so the shared head and tail of
// the subject are said once on the card and each row carries only the part that varies. Stacking
// the members instead makes the choice a memory exercise — on the live catalogs two branch
// candidates differ by `pref` against `prim`, four characters, 640px apart.
function CandidateComparison({ items, chosen, onChoose }: {
  items: GovernanceQueueItem[]
  chosen: string | null
  onChoose: (factKey: string) => void
}) {
  const parts = varyingParts(items.map(item => item.subject))
  const shared = parts.prefix || parts.suffix
  // A tie used to be narrated here ("nothing on file separates these N: they rank the same and
  // their types were matched the same way"). The Rank and Type match columns sit side by side one
  // line below, identical, for anyone to see. Prose restating two adjacent columns is not a
  // finding — it is the interface reading its own table aloud.
  return (
    <div className="gq-compare">
      {shared && (
        <p className="mono gq-compare-shared" data-testid="gq-compare-shared">
          {parts.prefix}
          <span className="gq-compare-slot" aria-hidden="true">…</span>
          {parts.suffix}
        </p>
      )}
      <div className="gq-compare-scroll">
        <table className="gq-compare-table" data-testid="gq-compare">
          <thead>
            <tr>
              <th scope="col">{shared ? 'Differs by' : 'Candidate'}</th>
              <th scope="col">Rank</th>
              <th scope="col">Type match</th>
              <th scope="col">Human review</th>
              <th scope="col">Decide</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, i) => (
              <tr key={item.fact_key} data-testid={`gq-compare-row-${item.fact_key}`}>
                <td className="mono gq-compare-varies" data-testid="gq-compare-varies">
                  {parts.middles[i]}
                </td>
                {/* The one field that ranks one candidate over another. It is not a probability
                    and the column says so once, in the caption, rather than on every row. */}
                <td className="gq-compare-rank" data-testid="gq-compare-rank">
                  {typeof item.detail.strength === 'number'
                    ? item.detail.strength
                    : 'not recorded'}
                </td>
                <td className="gq-compare-basis" data-testid="gq-compare-basis">
                  {TYPE_BASIS[asStr(item.detail.type_basis)]?.label
                    ?? (asStr(item.detail.type_basis)
                      ? asStr(item.detail.type_basis).replaceAll('_', ' ')
                      : 'not recorded')}
                </td>
                <td className="gq-compare-review" data-testid="gq-compare-review">
                  {item.state}
                </td>
                {/* The table is where the choice is made, never a second way to make it: choosing
                    here opens THAT candidate's confirmation, and the agreement is still ticked
                    explicitly there. A row the server withholds confirm on offers nothing and
                    says why, exactly as the dossier does. */}
                <td className="gq-compare-decide">
                  {item.available_actions.includes('confirm') ? (
                    <button
                      type="button"
                      className="btn q-ghost gq-compare-btn"
                      aria-expanded={chosen === item.fact_key}
                      onClick={() => onChoose(item.fact_key)}
                    >
                      {chosen === item.fact_key ? 'Close' : 'Confirm…'}
                    </button>
                  ) : (
                    <span className="gq-why" data-testid="gq-compare-why">
                      {withheldReason(item)}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── one candidate group ──────────────────────────────────────────────────────────────────────────

// Several bridges proposing the SAME entity between the SAME two catalogs are the cross-product of
// two facts, not several findings, so they get one card and one judgement. Reject-all fans out to
// one governed command per key server-side (each with its own audit row and its own savepoint),
// which is why partial outcomes are ordinary and the result is reported as a split.
function CandidateGroup({ entry, onDone, onConflict, outcome }: {
  entry: Entry
  onDone: (message: string, factKey?: string) => void
  onConflict: (detail: string) => void
  outcome: { factKey: string; message: string } | null
}) {
  const [open, setOpen] = useState(false)
  // The single candidate chosen from the comparison table, opened on its confirmation. Independent
  // of `open` (which shows every dossier), so choosing one does not dump the other four on screen.
  const [chosen, setChosen] = useState<string | null>(null)
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
      {/* No explanatory note. "The cross-product of the same two facts" is internal vocabulary,
          and "compare them below" narrates a table already on screen with a Confirm on every row.
          The headline names the choice; the table is the comparison. */}
      {skipped > 0 && (
        <p className="gq-group-note" data-testid="gq-group-settled">
          {skipped} of these {count} {one ? 'is' : 'are'} already endorsed. The server offers no
          rejection on {one ? 'it' : 'them'}, so a group rejection here settles the other{' '}
          {rejectable.length} and leaves {one ? 'that one as it is' : 'those as they are'}.
        </p>
      )}
      <CandidateComparison
        items={items}
        chosen={chosen}
        onChoose={key => setChosen(current => (current === key ? null : key))}
      />
      {/* The automatic axis, once, and ONLY when there is a verdict to state. `production_
          eligibility` is null when the payload has nothing to derive it from, and a card-level
          line reading "Not evaluated" is an absence rendered as standing text — exactly what made
          this page read as substance while reporting nothing. The per-ROW axis still states the
          absence explicitly, because there the distinction between "not assessed" and "passed"
          is the whole point; a group card repeating the null is not. */}
      {items.some(item => !(item.production_eligibility_code in EXECUTION_ABSENT)) && (
        <p className="hint gq-group-exec" data-testid="gq-group-exec">
          Automatic execution safety, across all {count}:{' '}
          {shared(item => item.production_eligibility ?? 'not evaluated')}
        </p>
      )}

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

      {(open || chosen !== null) && (
        <div role="group" aria-label={`The ${count} candidate links in this group`}>
          <ul className="rows gq-members">
            {(open ? items : items.filter(item => item.fact_key === chosen)).map(item => (
              <li className="row q-item gq-member" key={item.fact_key}>
                <QueueRow
                  item={item}
                  onDone={onDone}
                  onConflict={onConflict}
                  initialPanel={item.fact_key === chosen ? 'confirm' : 'none'}
                  outcome={outcome?.factKey === item.fact_key ? outcome.message : undefined}
                />
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
  // The last row-scoped outcome, kept by fact_key so it survives the reload that follows it.
  const [outcome, setOutcome] = useState<{ factKey: string; message: string } | null>(null)
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

  // WHERE THE OUTCOME LANDS. A row decision reports back on its own row: this banner used to be
  // the only feedback and it renders directly under the page purpose, so confirming something near
  // the foot of the queue painted the result thousands of pixels above the viewport, where nobody
  // who just clicked would see it. A GROUP decision spans many facts and has no single row, so it
  // still uses the banner — as does a row decision whose fact has since left the open list.
  function onDone(message: string, factKey?: string) {
    if (factKey) {
      setOutcome({ factKey, message })
      setNotice('')
    } else {
      setOutcome(null)
      setNotice(message)
    }
    void load()
  }

  function onConflict(detail: string) {
    setOutcome(null)
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
        <p role="alert" className="error">
          {error}
        </p>
      </section>
    )
  }

  if (!queue) {
    return (
      <section className="gq">
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
  // WHICH chips exist comes from the payload (so a kind this client has never seen still gets one);
  // what each chip COUNTS is computed over the other axis's filter. The counts used to come
  // straight from the payload maps while the summary counted the filtered list, so one click on a
  // catalog put the same quantity on screen twice with two different values.
  const kindCount = (kind: string): number => queue.items.filter(item =>
    item.kind === kind && (catalog === null || item.catalogs.includes(catalog))).length
  const catalogCount = (slug: string): number => queue.items.filter(item =>
    item.catalogs.includes(slug) && (kindFilter === null || item.kind === kindFilter)).length
  const shownKinds = kindFilter === null ? kinds : kinds.filter(kind => kind === kindFilter)
  // A kind with nothing waiting AND nothing we failed to read is settled: it collapses into one
  // named line. A kind we could not look at is not settled and keeps its own section, because
  // "nobody is waiting on you" and "we could not see" must never render as the same sentence.
  const settledKinds = shownKinds.filter(kind =>
    items.every(item => item.kind !== kind)
    && !queue.unreadable.some(entry => unreadableListings(kind).includes(entry.listing)))
  const workingKinds = shownKinds.filter(kind => !settledKinds.includes(kind))
  const yours = items.filter(item => item.available_actions.includes('confirm')).length
  const elsewhere = items.filter(item =>
    item.available_actions.length > 0 && !item.available_actions.includes('confirm')).length
  const endorsed = items.filter(item => item.available_actions.length === 0).length
  const total = items.length
  // A row outcome shows on its row. If that fact has left the list — a rejection usually takes it
  // out — there is no row to carry it, and dropping it would leave the reviewer with no answer at
  // all, so the banner picks it up instead.
  const outcomeOnScreen = outcome !== null
    && items.some(item => item.fact_key === outcome.factKey)
  const banner = outcomeOnScreen ? '' : (notice || outcome?.message || '')

  return (
    <section className="gq">
      {banner && (
        <p role="status" className="callout callout--accent gq-notice" data-testid="gq-notice">
          {banner}
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

      {/* A BURN-DOWN, NOT A SCOREBOARD.
          This began as ten identical tiles over two denominators — four counting decisions by
          status and six counting the same decisions by kind — so reading across and adding
          produced a number that means nothing, and six of the ten read zero. Cutting it to four
          tiles fixed the arithmetic but kept the shape wrong: three of the four numbers were one
          fact decomposed (yours + elsewhere = waiting), the fourth was not work at all, and all
          four sat at identical weight.
          This is a queue a person works THROUGH, and `onDone` refetches, so the counts move as
          they go. The bar is the one thing four tiles could not show — the proportion — and it
          retreats as the work is done. */}
      {total > 0 && (
        <div className="gq-summary" data-testid="gq-summary">
          <p className="gq-burn-count" data-testid="gq-burn-count">
            <b>{yours}</b> to decide
          </p>
          <div className="gq-burn">
            {/* Below four items a segmented bar is a solid block claiming to visualise a split
                that has no shape, so it is simply absent and the legend carries everything. */}
            {total >= 4 && (
              <div className="gq-burn-bar" data-testid="gq-burn-bar" aria-hidden="true">
                <span className="gq-seg" data-seg="yours" style={{ flexGrow: yours }} />
                <span className="gq-seg" data-seg="elsewhere" style={{ flexGrow: elsewhere }} />
                <span className="gq-seg" data-seg="endorsed" style={{ flexGrow: endorsed }} />
              </div>
            )}
            {/* Every segment's number in words: the bar is aria-hidden, so this is the ONLY
                carrier for anyone who cannot see it, and colour is never the signal on its own. */}
            <p className="gq-burn-legend" data-testid="gq-burn-legend">
              <span className="gq-key" data-seg="yours">{yours} yours</span>
              <span className="gq-key" data-seg="elsewhere">
                {elsewhere} need{elsewhere === 1 ? 's' : ''} someone else
              </span>
              <span className="gq-key" data-seg="endorsed">{endorsed} endorsed</span>
            </p>
          </div>
        </div>
      )}
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
              {catalogLabel(slug)} ({catalogCount(slug)})
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
              {kindLabel(kind)} ({kindCount(kind)})
            </button>
          ))}
        </div>
        {/* NOTHING ANNOTATES THE CHIPS. They are filters; nobody sums a filter's counts against the
            queue length, so the fact that a cross-catalog link is counted under both its catalogs
            needs no defending. A note here would be the same species as the "scope-relative"
            disclaimer it replaced — the interface apologising for its own display. If a number
            needs a paragraph to defend it, the fix is the number's presentation. */}
      </div>

      {/* A kind with nothing waiting renders NOTHING: its chip carries the zero, which is the whole
          honest statement. The prose that used to stand here also claimed more than the payload
          knows — `items_visible_to_you_by_kind` counts OPEN items, so a zero cannot tell
          "everything was decided" from "nothing was ever proposed". A kind we could not READ is a
          different claim entirely and keeps its own section below. */}
      {workingKinds.map(kind => {
        const forKind = items.filter(item => item.kind === kind)
        const entries = byUrgency(entriesFor(kind, forKind))
        const broken = queue.unreadable.filter(entry =>
          unreadableListings(kind).includes(entry.listing))
        const where = catalog === null ? 'the catalogs you can see' : catalogLabel(catalog)
        return (
          <section className="gq-kind" key={kind} data-testid={`kind-${kind}`}>
            {/* h2, not h3. The kind sections are top-level regions of this screen, exactly like
                the two panels below them — at h3 they read as subsections of a heading that does
                not exist, and the outline jumped back UP to h2 partway down the page. */}
            <h2 className="gq-kind-head">
              {kindLabel(kind)} <span className="tabular-nums">{forKind.length}</span>
            </h2>
            {KIND_ABOUT[kind] && (
              <p className="hint gq-kind-about" data-testid="gq-kind-about">{KIND_ABOUT[kind]}</p>
            )}
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
                      ? (
                          <CandidateGroup
                            entry={entry}
                            onDone={onDone}
                            onConflict={onConflict}
                            outcome={outcome}
                          />
                        )
                      : (
                          <QueueRow
                            item={entry.items[0]}
                            onDone={onDone}
                            onConflict={onConflict}
                            outcome={outcome?.factKey === entry.items[0].fact_key
                              ? outcome.message
                              : undefined}
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
