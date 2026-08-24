// One guided feature-generation flow: a gates strip naming who holds each step of the loop, a
// goal + scope hero with two peer paths (Generate candidate sets through the engine, or Write
// definitions myself through the batch composer), set summary cards to compare strategy lenses,
// one shared candidate list, and a selection tray with an explicit approval confirm before
// anything registers.
//
// Invariants carried over from the hardening campaign:
// - Lineage comes ONLY from backend-resolved pairs (FeatureIdea.derives_pairs for generated
//   candidates, the drafted-against source snapshot for drafts), never from typed context.
// - registerFeature fires only after the explicit Confirm approval step, exactly once per
//   candidate (batch in-flight ref + per-candidate registered state).
// - Every fetch handler carries an out-of-order guard (monotonic sequence refs).
// - Scope edits invalidate candidates: source edits clear everything (draft snapshots no longer
//   match the context), target edits clear generated candidates, sets, and rejections.
//
// E4 cutover (2026-08-14): the engine is the ONLY generation path, and it plans over ONE frozen
// catalog context. An entity-only (cross-catalog) request is refused typed by the route
// (422 SEMANTIC_REQUIRES_CATALOG_SOURCE), so the form no longer COLLECTS an entity — a field
// whose only reachable outcome is a refusal is an invitation to fail. The API opt survives in
// api.ts (the wire field is still accepted); the screen simply never fills it.
//
// Multi-set model decisions (documented for the record):
// - Generation always calls /features/recommend-sets. There is NO silent fallback to
//   /features/recommend on a 503: both routes stand behind the same generation capability, so a
//   retry there would only produce a second copy of the same refusal. The server's own sentence
//   renders instead (never fake capability, and never a cause this screen guessed — see `fail`).
// - A response with one non-empty set renders the flat list exactly as before, no cards row.
// - Sets that came back empty are dropped from the compare row (nothing to take or compare);
//   their gauntlet rejections still show in the rejections panel.
// - A considered candidate is identified by the backend's opaque option_id, not its display name.
//   Same-name recipe, governed, and free-form variants remain separate rows and retain their own
//   provenance and draft identity. Legacy responses without option_id fall back to the per-fetch name.
// - Set theses are client-side copy keyed by the router's fixed lens vocabulary (the wire
//   carries no set description); an unknown lens simply renders without a thesis line.
//
// Feedback channels (Phase 3 decisions, documented for the record):
// - In confirmation-required mode, whole-round feedback first creates a new sealed recognition over
//   the ROUND's original hypothesis/objective plus the bounded human instruction. The user confirms
//   that revised scope before /contract/considered-set mints the superseding run. Emergency legacy
//   mode retains the direct one-shot call.
// - Pin semantics are client-side: candidates that are selected or registered stay (selected
//   pins get a Kept chip; registered rows are already their own mark), everything else is
//   replaced by the new response. A new candidate reusing a pinned name is dropped: the pin
//   wins, one row per name. Kept candidates leave the sets model (lenses cleared) so new set
//   cards count only their own features; kept rows stay visible in every set view. Drafts are
//   not part of the engine round and pass through untouched.
// - Both feedback channels stop at 3 rounds, then it is back in the human's hands. Scope edits
//   reset the counters with everything else; a fresh generate starts a fresh allowance.
// - Per-candidate feedback (refine) exists on generated rows only: a draft is the human's own
//   definition, revised by editing its line, not by asking the engine. Registered rows take no
//   feedback, and a revision that resolves after its candidate registered is dropped.
// - A refine revision is a SUGGESTION: it touches nothing until Approve revision, and even then
//   only the local candidate; registration still requires the tray's explicit confirm.
// - Simple mutual-exclusion rule with registration: while the tray is confirming or a register
//   batch is in flight, both feedback channels are disabled, Approve revision / Revert to
//   original are inert, and the generate path plus scope fields lock, so nothing can change a
//   row or pull it out of view while its registration is being written.
// - Scope edits bump the generation sequence so an in-flight round (generate or feedback) that
//   resolves after the edit is discarded, never applied against the new scope.
import { Fragment, type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError, type ConsideredSetResp, type FeatureFreshness, type FeatureIdea, type FeatureSpecIn,
  type OptionActionsEntry,
  type IntakeReading, type IntakeResp, type NeedsSetupCandidate,
  type JoinStep, type RankedRecipe, type Recipe, type RecipeDisposition, type RecognitionCandidate,
  type RecognitionResp, type RefineRejection, type Rejection, type SetRecommendation,
  contractConfirm, contractConsideredSet, contractDraft, contractIntake, contractIntakeTarget,
  contractRecognitions, featureFreshness,
  featureRecipe, refineCandidate, registerFeature,
  contractUoaProposal,
  type UoaOption,
  type UoaProposalResp,
  contractOptionDetail,
  type OptionDetailResp,
  type OptionDecisionRecord,
  type FormulaDraftStatus,
} from '../api'
import { FormulaDraftAction } from './FormulaDraftAction'
import { getSession } from '../session'

// ---- Phase 1B feature flags -------------------------------------------------------------------
// Scope confirmation is the release-safe default. Setting the flag to "0" is an emergency UI
// compatibility switch only; the backend still rejects a one-shot request unless its own explicit
// legacy mode is active. Read at call time so tests can exercise both deployment modes.
// - intent_confirmation_ui: on Generate, first recognise the objective and let the human
//   confirm/override/broaden the scope BEFORE the considered set is generated.
// - intent_disposition_lens: when a scoped response carries dispositions, group recipes by their
//   final disposition (only meaningful with the confirmation UI on).
function confirmationUiEnabled(): boolean {
  return import.meta.env.VITE_INTENT_CONFIRMATION_UI !== '0'
}
function dispositionLensEnabled(): boolean {
  return import.meta.env.VITE_INTENT_DISPOSITION_LENS === '1'
}
// Phase 2A (independent of the confirmation/lens flags): render the deterministic presentation-
// priority ranking when a scoped response carries it. Default OFF → the ranked panel never renders
// and the screen is byte-identical to Phase 1B, even when a response happens to carry `ranking`.
function rankingEnabled(): boolean {
  return import.meta.env.VITE_INTENT_RANKING === '1'
}

// Display text for the ranker's structured reason codes, mapped IN THE FRONTEND (never relying on
// backend text). Two SEPARATE streams that must stay visually distinct: RANK_REASON_TEXT explains a
// recipe's canonical position (positive AND negative factors); INITIAL_VIEW_REASON_TEXT explains its
// initial-view membership (why a non-initial recipe was held back). Mirrors the RejectCode pattern:
// an unknown code from a newer backend still renders as words, never breaks the client.
const RANK_REASON_TEXT: Record<string, string> = {
  primary_use_case_match: 'Matches your primary use case',
  supporting_match: 'Supports your use case',
  required_context_match: 'Matches the required modelling context',
  exact_binding: 'Binds exactly to your columns',
  pit_complete: 'Point-in-time complete',
  high_explainability: 'Highly explainable',
  low_binding_quality: 'Weaker column binding',
  pit_metadata_incomplete: 'Point-in-time metadata incomplete',
  entity_grain_unknown: 'Entity grain unknown',
}
const INITIAL_VIEW_REASON_TEXT: Record<string, string> = {
  selected_initial_view: 'Shown in the starting view',
  duplicate_variant_not_in_initial_view: 'A similar variant is already shown',
  family_cap_not_in_initial_view: 'Its recipe family is already well represented',
  ambiguous_binding_not_in_initial_view: 'Its column binding is ambiguous',
  stage_diversity: 'Held back to keep the starting view diverse',
}
function humanizeCode(code: string): string {
  return code.replace(/_/g, ' ')
}
function rankReasonText(code: string): string {
  return RANK_REASON_TEXT[code] ?? humanizeCode(code)
}
function initialViewReasonText(code: string): string {
  return INITIAL_VIEW_REASON_TEXT[code] ?? humanizeCode(code)
}

// Phase-2B: the CLOSED modelling-context vocabulary the human may confirm/add at Gate #1. Hardcoded
// FRONTEND-side (mirrors RANK_REASON_TEXT), tracking the backend's stable 8-member MODELLING_CONTEXTS
// set. A SOFT dimension: these are ranking nudges only — nothing here narrows scope or rejects a recipe.
const MODELLING_CONTEXT_OPTIONS = ['ifrs9', 'frtb', 'xva', 'lcr', 'nsfr', 'lgd', 'irrbb', 'ftp'] as const
// Display text for the ranker's per-recipe SOFT-dimension signal warnings, mapped IN THE FRONTEND
// (never backend text). A warning is presentation-only — a badge the human sees, never a rejection.
// An unknown code from a newer backend still renders as words (humanizeCode), never breaks the client.
const SIGNAL_WARNING_TEXT: Record<string, string> = {
  entity_grain_mismatch: 'Built at a different grain — derivable by roll-up',
  modelling_context_conflict: 'Declares a different modelling context',
}
function signalWarningText(code: string): string {
  return SIGNAL_WARNING_TEXT[code] ?? humanizeCode(code)
}

// ── Gate #1: what actually happened to this recognition (repair seam, Task 5) ────────────────────
//
// The 2026-08-15 incident put four different outcomes behind ONE sentence — "No use-case was
// recognised for this objective" — which was true of exactly one of them. It was shown for a
// technical failure, for a genuine "nothing in the taxonomy matched", for an ambiguous answer whose
// alternatives were sitting in the response unrendered, and (once the platform learned to keep a
// valid candidate away from a sloppy sibling) for a partial recovery that HAD a scope.
//
// Two independent lines, because they answer two different questions and a user can need both:
//   * `scopeNotice` — what you are looking at. Absence first: a platform failure and an answer of
//     "nothing matched" are not the same thing, and neither is an answer with alternatives but no
//     designated primary.
//   * `qualityNotice` — what it cost to get here. A discarded proposal or a correction the model was
//     asked for; silent when the first answer simply validated.
// Both are PLATFORM text keyed off closed vocabularies. The recognizer's own `ambiguity_note` is on
// the wire but deliberately not rendered as either: on a failure arm it is a diagnostic naming a
// rule code, which is support material, not a sentence for the person confirming a scope.

function scopeNotice(rec: RecognitionResp, hasPrimary: boolean): string | null {
  if (rec.status === 'technical_failure')
    return 'Recognition could not be validated; you may broaden to all recipes.'
  if (rec.status === 'unscoped')
    return 'No governed use case clearly matched. Show all buildable recipes to generate over everything.'
  if (hasPrimary) return null
  if (rec.candidates.length > 0)
    return 'Several alternatives were found; choose one as the primary, or broaden to all recipes.'
  return 'No use-case was recognised for this objective. Show all buildable recipes to generate over everything.'
}

function qualityNotice(rec: RecognitionResp): string | null {
  const quality = rec.recognition_quality
  // No block at all = an attempt stored before the platform recorded this (migration 1071), or a
  // backend older than this client — the two are indistinguishable here and want the same answer.
  // Saying nothing is the honest render: "the model answered first time" and "nobody wrote down
  // whether it did" are different facts, and only one of them is knowable. Nullish, not `=== null`:
  // an absent key is the shape an older backend actually sends.
  if (quality == null) return null
  if (quality.dropped_candidate_count > 0) {
    // Reachable on `partially_recovered` and, rarely, on an `unscoped` answer that also lost a
    // candidate — the loss is reported either way. A drop is only ever recorded when something
    // SURVIVED the partition (a result with no survivors keeps today's fail-open shape and records
    // no drops), so "the remaining scope" always names something the user can actually see.
    return quality.dropped_candidate_count === 1
      ? 'One invalid proposal was discarded; review the remaining scope.'
      : `${quality.dropped_candidate_count} invalid proposals were discarded; review the remaining scope.`
  }
  if (quality.disposition === 'repaired')
    return 'The first answer did not validate; the model was asked to correct it. Review the scope before confirming.'
  return null
}

// ── T7 (a)–(c): the prediction target, said out loud ────────────────────────────────────────────
//
// THE ACKNOWLEDGMENT DISCRIMINATOR. Confirming a target the concept registry does not certify as
// an outcome label is a typed 422 from /contract/intake/target, written PER TIER — near_label
// earns the word "proxy" because the registry positively asserts label-adjacency; standard says
// the registry looked and declassified; unregistered says absence asserts nothing either way. The
// screen renders that sentence VERBATIM and composes none of its own: one banner covering all
// three would re-create in the UI exactly the over-claim the backend removed (it had been telling
// 338 of 359 concepts they were proxies for an outcome nobody measured them against).
//
// The refusal carries no error_code, so the discriminator is the server's own closing instruction,
// which names the field to re-send. Deliberately fail-closed: if that wording ever moves, the
// sentence still renders verbatim and the acknowledge control simply does not appear — the flow
// degrades to "read this and pick another target", never to a silent acknowledgment.
const NOT_OUTCOME_ACK_FIELD = 'target_not_outcome_acknowledged'

// One line per registry class, keyed off the SERVER's closed `target_leakage_class` vocabulary —
// the same pattern as BINDING_STATE_LABEL below, and each line is the api.ts contract's own
// wording for that tier, not a paraphrase invented here. Three lines, never one: `standard` is the
// OPPOSITE claim to `near_label`, and the absent case is a third thing again. The authoritative
// words at the moment of commitment are still the 422's, rendered verbatim beside the control.
const LEAKAGE_CLASS_LINE: Record<string, string> = {
  outcome: 'The registry certifies this concept as an outcome label — the answer itself.',
  near_label: 'The registry marks this concept near_label: it borders the outcome label, so a '
    + 'model trained on it can read its own answer back.',
  standard: 'The registry looked at this concept and did not certify it as an outcome label. '
    + 'Nothing here says it correlates with one either.',
}
// `target_leakage_class: null` — the column carries no registered concept. Silence, in both
// directions: never rendered as a proxy, and never rendered as safe.
const LEAKAGE_UNREGISTERED_LINE =
  'This column carries no registered concept, so nothing certifies it as an outcome label — and '
  + 'absence is not an assertion the other way either.'

// T7 (b) — where the label window came from, or why there is none. Four outcomes, four sentences,
// and NO INVENTED NUMBER anywhere: a `stated` source with `target_window_days: null` is the
// degraded month-horizon case (a month is 28, 29, 30 or 31 days, so the goal states a horizon this
// code may not count), and it says exactly that rather than picking one. `contradicted` never
// reaches here — the server's own `window_refusal.detail` names both numbers and is rendered
// verbatim instead.
function windowLine(source: string, days: number | null): string {
  if (source === 'stated') {
    return days !== null
      ? `Label window: ${days} days — the horizon your goal states.`
      : 'Label window: your goal states a horizon, but not one that can be counted in days.'
  }
  if (source === 'model_only' && days !== null) {
    return `Label window: ${days} days — read from your description. Your goal states no horizon `
      + 'to check it against.'
  }
  // `unstated`, and the defensive fall-through for a model_only with no number to show.
  return 'Label window: no horizon stated, and none was read. Nothing is assumed.'
}

// The disposition lens, in render order: each final_disposition mapped to its human heading.
const DISPOSITION_GROUPS: { key: RecipeDisposition['final_disposition']; heading: string }[] = [
  { key: 'eligible', heading: 'Recommended' },
  { key: 'grounding_incomplete', heading: 'Grounding incomplete' },
  { key: 'unbuildable', heading: 'Relevant but missing data' },
  { key: 'safety_rejected', heading: 'Rejected by safety' },
  { key: 'out_of_scope', heading: 'Outside confirmed scope' },
]

// The reason a recipe landed in its group: the eligible tier, or the reason_codes of the stage
// that decided it (applicability for out-of-scope, grounding for unbuildable, safety for rejected).
function dispositionReason(d: RecipeDisposition): string {
  if (d.final_disposition === 'eligible') {
    return d.relevance_tier ? `${d.relevance_tier} relevance` : 'eligible'
  }
  const stage = d.final_disposition === 'out_of_scope'
    ? d.applicability
    : d.final_disposition === 'unbuildable' || d.final_disposition === 'grounding_incomplete'
      ? d.grounding
      : d.safety
  const codes = stage?.reason_codes ?? []
  return codes.length > 0 ? codes.join(', ') : d.final_disposition.replace(/_/g, ' ')
}

// ── Slice 1: ONE derived view phase, and the shell renders from it ───────────────────────────────
//
// The screen used to decide visibility panel by panel, so the state strip could say "Compare" while
// the page still led with the intake form and pushed the results below the fold. There is now a
// single discriminated value. It is DERIVED — no new source of truth, no phase state to keep in
// sync — and it owns the SHELL (which header renders, what the stage heading says, what the next
// action is). Panel CONTENT stays data-driven: rejections render because there are rejections, not
// because of a phase.
type WorkbenchPhase =
  | 'draft'         // no brief has landed: the intake form is the page
  | 'planning'      // a run is in flight and nothing is on screen to decide about yet
  | 'scope_review'  // a recognition is waiting on the human's Gate #1 confirmation
  | 'compare'       // candidates are on screen and nothing is picked
  | 'approve'       // candidates are picked and the tray's confirm is the next act
  | 'complete'      // something was registered or governed and nothing is pending
  | 'empty'         // a round landed and returned no candidates
  | 'error'         // a run failed; the notice is the whole story

interface PhaseInput {
  // Recognition OUTRANKS `generating` on purpose: while the human's confirmed scope is being
  // grounded the recognition panel is still the thing on screen, so the phase must not flip to
  // planning and pull the panel out from under the click that started it.
  awaitingScope: boolean
  generating: boolean
  candidateCount: number
  // A round landed and returned zero candidates (distinct from "no round has run", which is null).
  roundReturnedNothing: boolean
  selectedCount: number
  settledCount: number
  hasNotice: boolean
}

function derivePhase(s: PhaseInput): WorkbenchPhase {
  if (s.awaitingScope) return 'scope_review'
  if (s.generating) return 'planning'
  if (s.candidateCount > 0) {
    if (s.selectedCount > 0) return 'approve'
    return s.settledCount > 0 ? 'complete' : 'compare'
  }
  if (s.roundReturnedNothing) return 'empty'
  return s.hasNotice ? 'error' : 'draft'
}

// The stage heading + the one sentence under it. Facts and the next act, never instruction (the
// draft shell keeps the teaching copy). Phrased in the app's own vocabulary: candidates are
// generated, selections are picked, registration saves, governance signs.
//
// `draft` and `error` are the two phases that render the intake FORM rather than the deck, so
// their entries never reach the screen today. They stay because the map is total over the phase
// union — a later slice that gives the draft shell its own deck inherits copy, not a hole.
const STAGE_COPY: Record<WorkbenchPhase, { title: string; next: string }> = {
  draft: {
    title: 'State the goal',
    next: 'Describe what you are predicting, then generate candidates or write definitions yourself.',
  },
  planning: {
    title: 'Planning over the catalog',
    next: 'The engine is grounding recipes against this catalog. Nothing is saved by this step.',
  },
  // "Confirm scope" is the review's own stage verb, and it is deliberately NOT the panel's own
  // heading ("Confirm the scope"): one phase owns the page headline, and two identical <h2>s would
  // make the deck a second title for the same thing.
  scope_review: {
    title: 'Confirm scope',
    next: 'Nothing generates until you confirm, adjust, or broaden the recognised scope below.',
  },
  compare: {
    title: 'Compare and refine',
    next: 'Take a set, pick candidates across sets, or tell the engine what to change.',
  },
  approve: {
    title: 'Ready for your approval',
    next: 'Approve and register writes your selected definitions, under your name.',
  },
  // Deliberately does NOT name the act: a round can settle by registration, by governance, or by
  // both, and the summary must not claim the one that did not happen. The row chips carry the
  // precise word ("Registered feat_01", the contract id) where it is true of that item.
  complete: {
    title: 'Nothing waiting on you',
    next: 'Your saved items are marked in the list. Keep picking from this round, or revise the '
      + 'brief to generate a new one.',
  },
  empty: {
    title: 'No candidates for this brief',
    next: 'Nothing grounded against this catalog. Revise the brief and generate again.',
  },
  error: {
    title: 'The run did not complete',
    next: 'The notice above is what the platform reported. Adjust the brief and try again.',
  },
}

// ── The composition of THIS run ─────────────────────────────────────────────────────────────────
//
// Counted in the FRONTEND from the per-option `binding_state` the server already sends
// (recommended_options + actionable_options). No number here is authored: a run that returned four
// options describes four options. The labels say what each state MEANS, because a bare count of
// "ambiguous" reads as a failure when it is a piece of confirmation work nobody has done yet.
const BINDING_STATE_ORDER = ['bound', 'ambiguous', 'missing', 'blocked'] as const
const BINDING_STATE_TEXT: Record<string, string> = {
  bound: 'every operand resolved — ready to compare',
  ambiguous: 'a proposed meaning needs confirming before these can bind',
  missing: 'this catalog holds no column the recipe needs',
  blocked: 'refused by a named rule, not a gap in your data',
}
const BINDING_STATE_LABEL: Record<string, string> = {
  bound: 'Bound',
  ambiguous: 'Ambiguous',
  missing: 'Missing operands',
  blocked: 'Structurally blocked',
}

interface CompositionRow {
  state: string
  label: string
  meaning: string | null
  count: number
}

// Tally binding states in the authored order first, then any state this client does not know —
// an unknown state from a newer backend renders as words with NO invented meaning, never crashes
// and never silently drops an option from the total.
function composeBindingStates(entries: OptionActionsEntry[]): CompositionRow[] {
  const counts = new Map<string, number>()
  for (const entry of entries) {
    counts.set(entry.binding_state, (counts.get(entry.binding_state) ?? 0) + 1)
  }
  const rows: CompositionRow[] = []
  for (const state of BINDING_STATE_ORDER) {
    const count = counts.get(state)
    if (count === undefined) continue
    counts.delete(state)
    rows.push({
      state, label: BINDING_STATE_LABEL[state] ?? humanizeCode(state),
      meaning: BINDING_STATE_TEXT[state] ?? null, count,
    })
  }
  for (const [state, count] of [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    rows.push({ state, label: humanizeCode(state), meaning: null, count })
  }
  return rows
}

const HELP_STYLE = { fontSize: 12 } as const
// Solid ok chip (index.css has no fresh badge class; mirrors .badge.stale's solid treatment).
const OK_SOLID_CHIP_STYLE = {
  background: 'var(--ok-solid)', borderColor: 'transparent', color: 'var(--chip-ink)',
} as const
// (Slice 2 retired TRAY_STYLE: the selection tray is no longer the last row of the candidate
// list. It lives in the decision rail, which is a grid column on desktop and a pinned bar under
// 768px — both expressed in index.css, so the row list needs no sticky child any more.)

// ── Slice 3: what a revised round does to the round already on screen ───────────────────────────
//
// THE POLICY, chosen and implemented: submitting a revision keeps the current results visible and
// untouched until the new request SUCCEEDS. A failed revision therefore costs nothing — the run
// the human was working in is still there, with their selections. The review permits the other
// arm (clear at submit, with a warning) and forbids only silent invalidation.
//
// The other arm is ONE boolean: flip this to true and `generate` drops the round before awaiting.
// The drawer's copy is derived from the same constant below, so the promise on screen and the
// behaviour can never drift apart — which is the only way this switch is safe to flip.
const CLEAR_RESULTS_AT_REVISE_SUBMIT: boolean = false

// Said BEFORE the human submits, because the review's one hard rule here is that invalidation is
// never silent. The replacement sentence is deliberately precise about what survives: a fresh
// engine round replaces the generated candidates and the picks among them (their keys carry the
// fetch sequence), while hand-written drafts and anything already registered are untouched. The
// tool that KEEPS picks is whole-round feedback, so the copy names it rather than leaving the
// human to discover the difference by losing a selection.
const REVISE_SUBMIT_COPY = CLEAR_RESULTS_AT_REVISE_SUBMIT
  ? 'Generating clears the results below straight away, and the new round replaces them when it '
    + 'lands. If it fails, you will be left with no candidates on screen.'
  : 'Generating leaves the results below on screen and unchanged until the new round lands. If '
    + 'the request fails, nothing here changes.'
const REVISE_REPLACE_COPY =
  'When it lands it replaces this round: the generated candidates below, and any you have '
  + 'selected but not yet registered, make way for the new ones. Definitions you wrote yourself '
  + 'stay on the list, and features you already registered stay registered. To keep your picks '
  + 'instead, use "Feedback on the whole round" below.'

const EXAMPLE_GOAL = 'predict churn'

// One definition per line. The newline-separated example teaches the batch shape at a glance.
const DESCRIBE_PLACEHOLDER =
  'One feature per line, e.g.\ntotal spend per customer over the last 90 days\ndays since last transaction'

const WARN_GLYPH = 'M8 2.5 1.5 13.25h13L8 2.5ZM8 6.75v3M8 12v.01'

// Plain-English thesis per router lens. Client-side copy: the wire carries lens + features only,
// and the lens vocabulary is fixed by the backend's deterministic router.
const LENS_THESES: Record<string, string> = {
  unary: 'Single-column transforms; flags, buckets, and scaled values of one column.',
  ratio: 'Ratios between numeric columns; how quantities relate, not how large they are.',
  aggregation: 'Aggregations over related rows via a join key; totals, counts, and averages.',
  temporal: 'Point-in-time and recency signals; how behavior moves over time.',
  distributional: 'Position within the peer group; how this entity compares to its cohort.',
}

// Both feedback channels stop here: 3 rounds of guidance, then it is back in the human's hands.
const FEEDBACK_ROUNDS = 3

// Human labels for gauntlet rejection codes. STALE reads "stale source"; every other code
// lowercases with spaces so even an unknown code from a newer backend reads as words.
//
// The four USE-gate codes are spelled out rather than left to the fallback, because the fallback
// would render "personal data policy required" as a verdict on the COLUMN when it is a statement
// about a policy nobody has written yet. Two of the four are things no approval can change and two
// are things somebody has to set up; the label says which, so a reviewer knows whether to abandon
// the idea or to go and ask someone. The backend's `reason` carries the specifics beside it.
const REJECT_LABELS: Record<string, string> = {
  STALE: 'stale source',
  PROTECTED_CHARACTERISTIC: 'protected characteristic',
  DESCRIPTIVE_OPERAND: 'descriptive column',
  PERSONAL_DATA_POLICY_REQUIRED: 'needs a personal-data policy',
  CURRENCY_POLICY_REQUIRED: 'needs a currency decision',
}

function rejectLabel(code: string): string {
  return REJECT_LABELS[code] ?? code.toLowerCase().replace(/_/g, ' ')
}

// Display form of a router lens token: "temporal" -> "Temporal".
function lensLabel(lens: string): string {
  return lens.charAt(0).toUpperCase() + lens.slice(1)
}

function CalloutGlyph({ d }: { d: string }) {
  return (
    <span className="callout-glyph" aria-hidden="true">
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={d} />
      </svg>
    </span>
  )
}

// Peer-path card icons (plus-in-circle for Generate, pencil for Write definitions). Decorative:
// the card title text carries the meaning.
function PathGlyph({ children }: { children: ReactNode }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

// Registered rows swap their checkbox for this ok mark; the "Registered <id>" text carries the
// state, so the glyph is decorative (color never works alone).
function CheckGlyph() {
  return (
    <span
      aria-hidden="true"
      style={{
        display: 'inline-flex', width: 32, height: 32, flex: 'none',
        alignItems: 'center', justifyContent: 'center', color: 'var(--ok)',
      }}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="m3 8.5 3.5 3.5L13 4.5" />
      </svg>
    </span>
  )
}

// ---------------------------------------------------------------- gates strip

type GateState = 'done' | 'active' | 'todo'

// Text form of each gate state for assistive tech: the visual encoding (check glyph, wash,
// dimming) never works alone.
const GATE_STATE_WORDS: Record<GateState, string> = {
  done: 'done', active: 'current step', todo: 'upcoming',
}

function Gate({ state, who, title, sub }: {
  state: GateState
  who: 'You' | 'Engine'
  title: string
  sub: string
}) {
  return (
    <div
      className="gate"
      role="listitem"
      data-state={state}
      aria-current={state === 'active' ? 'step' : undefined}
    >
      <span className={who === 'You' ? 'gate-who you' : 'gate-who engine'}>{who}</span>
      <div className="gate-title">
        {title}
        {state === 'done' && <span className="gate-check" aria-hidden="true">✓</span>}
      </div>
      <div className="gate-sub">{sub}</div>
      <span className="visually-hidden">{GATE_STATE_WORDS[state]}</span>
    </div>
  )
}

// ------------------------------------------------------- T7: what the ticket actually says ----
//
// Everything the intake ticket knows about the target that a person deciding needs — the label
// window and where it came from, the registry's classification of the column, the labels this
// catalog DOES hold, and the nearest proxies. On the 2026-08-24 AML run every one of these facts
// existed and none of them was rendered: the objective said "in the next 90 days", the ticket said
// 0, and the screen showed neither number.
function TargetTicketFacts({ intake }: { intake: IntakeResp }) {
  const t = intake.ticket
  const classLine = t.target_leakage_class === null
    ? LEAKAGE_UNREGISTERED_LINE
    : LEAKAGE_CLASS_LINE[t.target_leakage_class]
  const proxies = intake.proxy_candidate_details ?? []
  const outcomes = intake.outcome_candidate_details ?? []
  // The details list is the one-liner material for the same refs, in the same order. Reading the
  // class off `proxy_candidates` by position would break silently if either list were reordered,
  // so it is looked up by ref — and an absent entry renders as absent, never as 'standard'.
  const classByRef = new Map(t.proxy_candidates.map(c => [c.ref, c.leakage_class]))
  return (
    <div style={{ display: 'grid', gap: 6 }} data-role="target-facts">
      {/* T7 (b): a contradiction is the SERVER's typed refusal and names both numbers. Rendered
          verbatim — a screen that re-derived "90 vs 0" would be quoting itself. */}
      {t.window_refusal !== null ? (
        <p className="hint" style={{ margin: 0 }} data-role="window-refusal">
          {t.window_refusal.detail}
        </p>
      ) : (
        <p className="hint" style={{ margin: 0 }} data-role="window-source">
          {windowLine(t.window_source, t.target_window_days)}
        </p>
      )}
      {t.target_column !== null && (
        <p className="hint" style={{ margin: 0 }} data-role="target-class">
          {t.target_concept
            ? <>Concept <code>{t.target_concept}</code> · </>
            : null}
          {classLine}
        </p>
      )}
      {/* THE LABEL THE MODEL DID NOT PICK. Naming it is the difference between an abstention and a
          shrug — and on the run this task came from, the catalog held one the whole time. */}
      {outcomes.length > 0 && (
        <div data-role="outcome-candidates">
          <p className="hint" style={{ margin: 0 }}>
            {outcomes.length === 1
              ? 'The catalog holds a true label:'
              : 'The catalog holds these true labels:'}
          </p>
          <ul className="hint" style={{ margin: '2px 0 0', paddingLeft: 18 }}>
            {outcomes.map(c => (
              <li key={c.ref}>
                <code>{c.ref}</code> · <span className="mono">{c.concept}</span>
                {c.ai_summary ? <> — {c.ai_summary}</> : null}
              </li>
            ))}
          </ul>
        </div>
      )}
      {/* The nearest proxies, each with the registry's OWN class beside it. The list is ranked, not
          chosen: it reports, it never retargets. A candidate the registry never classified shows
          'unregistered' — silence rendered as silence. */}
      {proxies.length > 0 && (
        <div data-role="proxy-candidates">
          <p className="hint" style={{ margin: 0 }}>Nearest proxies in this catalog:</p>
          <ul className="hint" style={{ margin: '2px 0 0', paddingLeft: 18 }}>
            {proxies.map(c => (
              <li key={c.ref}>
                <code>{c.ref}</code> · <span className="mono">{c.concept}</span>
                {' · '}
                <span className="mono">{classByRef.get(c.ref) ?? 'unregistered'}</span>
                {c.ai_summary ? <> — {c.ai_summary}</> : null}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

// ------------------------------------------------------------- T2: the needs-setup lane ------
//
// A CANDIDATE HELD BACK IS NOT A CANDIDATE THAT FAILED. These carry no card, no computation and no
// option id, because there is nothing to offer, save or govern until the binding is settled — but
// there IS work here, it belongs to a named person, and the run knows exactly what it is.
//
// The live arrangement this program came from returned ideas=0, actionable=0, needs_setup=114, and
// the screen answered it with "No grounded candidates for that goal. Rephrase the goal" — a wrong
// remedy over a hidden fact.
//
// EVERY SENTENCE IS THE SERVER'S. `sentence` is worded from the binder's own verdict status, and
// re-wording it here would re-assert absence over operands the catalog actually carries: on the
// FTR fixture 36 of 66 unbound required operands are `ambiguous`, meaning SEVERAL columns carry
// the concept and nobody has adjudicated between them. `tied_refs` names those columns, because
// they are what a person would be choosing between.
//
// It names no OTHER catalog, deliberately: the projection is handed one catalog and holds no
// cross-catalog inventory, so "monetary_flow lives in ftr" is a claim nothing here can make.
function NeedsSetupPanel({ entries, open, onToggle }: {
  entries: NeedsSetupCandidate[]
  open: boolean
  onToggle: () => void
}) {
  const n = entries.length
  return (
    <div className="rej-panel" data-testid="needs-setup">
      <div className="rej-line">
        <span className="badge tabular-nums">{n} need setup</span>
        <span>
          {n === 1 ? 'One candidate was' : `${n} candidates were`} planned but not served: a
          required input did not bind to a column in this catalog. This is binding work, not a
          rejection — nothing about the goal is wrong.
        </span>
        <button
          type="button"
          className="rej-toggle"
          aria-expanded={open}
          aria-controls="wb-needs-setup-list"
          onClick={onToggle}
        >
          {open ? 'Hide' : 'Show'}
        </button>
      </div>
      {open && (
        <ul className="rej-list" id="wb-needs-setup-list">
          {entries.map(entry => (
            <li key={entry.source_definition_id} style={{ display: 'grid', gap: 4 }}>
              <div>
                <code>{entry.name}</code>{' '}
                {/* Status-NEUTRAL by name: what these have in common is that they did not bind,
                    not that they are absent. What the binder found rides each operand below. */}
                <span className="hint">
                  unbound: {entry.unbound_concepts.join(', ')}
                </span>
              </div>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {entry.unbound_operands.map(operand => (
                  <li key={operand.role}>
                    <span className="hint">{operand.sentence}</span>
                    {operand.tied_refs.length > 0 && (
                      // The columns a human is choosing between, listed so the tie can actually
                      // be adjudicated. Dropping them turned "adjudicate this" into "onboard
                      // this data" — the wrong remedy, handed to the wrong owner.
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {operand.tied_refs.map(ref => (
                          <li key={ref}><code>{ref}</code></li>
                        ))}
                      </ul>
                    )}
                    {operand.resolution && (
                      <span className="hint"> — {operand.resolution}</span>
                    )}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- rejections panel

function RejectionsPanel({ rejections, open, onToggle }: {
  rejections: Rejection[]
  open: boolean
  onToggle: () => void
}) {
  const counts = new Map<string, number>()
  for (const r of rejections) {
    const label = rejectLabel(r.code)
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  // Largest tally first; ties keep first-seen order (stable sort).
  const tallyLine = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([label, n]) => `${label} ${n}`)
    .join(' · ')
  const n = rejections.length
  return (
    <div className="rej-panel">
      <div className="rej-line">
        <span className="badge rej-count tabular-nums">{n} rejected</span>
        <span>
          The safety gauntlet rejected {n} {n === 1 ? 'candidate' : 'candidates'} across all
          lenses: {tallyLine}.
        </span>
        <button
          type="button"
          className="rej-toggle"
          aria-expanded={open}
          aria-controls="wb-rej-list"
          onClick={onToggle}
        >
          {open ? 'Hide' : 'Show'}
        </button>
      </div>
      {open && (
        <ul className="rej-list" id="wb-rej-list">
          {rejections.map((r, i) => (
            <li key={`${i}:${r.name}`}>
              <code>{r.name}</code>
              <span className="badge rejected">{rejectLabel(r.code)}</span>
              <span className="rej-why">{r.reason}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// Uploaded cardinality is unvalidated free text. Only a normalized N:1 or 1:1 hop is known-safe;
// anything else ('1:N', '1:n', 'one_to_many', ...) can multiply rows, and a missing value means
// fan-out cannot be ruled out.
const SAFE_CARDINALITIES = new Set(['N:1', '1:1'])

function normalizeCardinality(raw: string | null): string | null {
  const value = raw?.trim().toUpperCase()
  return value ? value : null
}

function stepFansOut(raw: string | null): boolean {
  const norm = normalizeCardinality(raw)
  return norm !== null && !SAFE_CARDINALITIES.has(norm)
}

// Suggested feature name for a draft: a slug of the description, editable before selection.
function slugFrom(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 63)
}

// Candidate identity within a governed round is the server's opaque option_id. The fetch sequence
// isolates legacy responses and keeps a later revision from resurrecting state from an older round.
interface GeneratedCandidate {
  kind: 'generated'
  key: string
  idea: FeatureIdea
  // Every lens whose set holds this feature, in set order. Length > 1 renders the
  // "In N sets" chip.
  lenses: string[]
  // Pinned through a whole-round feedback regeneration (it was selected or registered when the
  // round replaced everything else). Kept rows show in every set view and leave the sets model.
  kept?: boolean
}

// SE-12 rule 5: requirements are TASKS with owners, not codes. The closed legacy vocabulary
// maps to a verb phrase; an unknown code from a newer backend renders as itself, never breaks.
function requirementTask(code: string): string {
  const tasks: Record<string, string> = {
    GRAIN_IS_UNIQUE: 'Profile uniqueness at the declared grain',
    TEMPORAL_IS_POPULATED: 'Verify the time column is populated (event history depth)',
    TEMPORAL_LAG_BOUNDED: 'Verify arrival lag stays inside the declared bound',
    JOIN_CONNECTIVITY: 'Govern the join this input rides',
    UNIT_CONSISTENT: 'Confirm a single unit of measure',
    CURRENCY_CONSISTENT: 'Declare the currency conversion policy',
    ADDITIVITY_SUPPORTS_OPERATION: 'Confirm the measure supports this aggregation',
    TYPE_IS_NUMERIC: 'Verify the column is numeric in the data',
  }
  return tasks[code] ?? code
}

function generationSourceLabel(idea: FeatureIdea): string {
  if (idea.path_authority === 'governed_cross_catalog') {
    return idea.recipe_id ? `Governed recipe · ${idea.recipe_id}` : 'Governed planner'
  }
  if (idea.generation_source === 'recipe') {
    return idea.recipe_id ? `Recipe · ${idea.recipe_id}` : 'Recipe'
  }
  if (idea.generation_source === 'user_defined') return 'User-defined'
  if (idea.generation_source === 'llm_intent') return 'Model intent'
  return 'Free-form'
}

// One recorded whole-round feedback submission: who asked, what they asked, what it did.
interface SetFeedbackRecord {
  round: number
  user: string
  instruction: string
  // Selected (unregistered) pins that were kept; registered rows persist as their own record.
  kept: number
  replaced: number
}

interface RecognitionTransition {
  feedback: string
  supersedesScopeId: string
}

// Per-candidate refine channel state, keyed by candidate key.
interface RefineState {
  open: boolean
  instruction: string
  busy: boolean
  // Rounds consumed (a gauntlet rejection consumes its round too; a transport error does not).
  rounds: number
  // The engine's revision awaiting the human's Approve revision / Revert to original call.
  pending: FeatureIdea | null
  pendingRound: number
  pendingInstruction: string
  rejection: RefineRejection | null
  error: string | null
  // Round number of the last APPROVED revision; renders the "Revised · R<n>" chip.
  appliedRound: number | null
}

const EMPTY_REFINE: RefineState = {
  open: false, instruction: '', busy: false, rounds: 0, pending: null, pendingRound: 0,
  pendingInstruction: '', rejection: null, error: null, appliedRound: null,
}

// Display form of backend-resolved lineage pairs; shared by the row facts and the refine diff.
function fmtPairs(pairs: [string, string][]): string {
  return pairs.map(([source, ref]) => `${source}:${ref}`).join(', ') || 'none'
}

// One field of the recorded revision diff: old struck through, new inserted, or the honest
// "unchanged" marker. <del>/<ins> carry the change semantics for assistive tech; strikethrough
// plus ordering keep the encoding non-color.
function DiffLine({ label, before, after }: { label: string; before: string; after: string }) {
  return (
    <p className="diff-line">
      <span className="diff-label">{label}</span>{' '}
      {before === after ? (
        <span className="diff-unchanged">unchanged</span>
      ) : (
        <>
          <del>{before}</del> <span aria-hidden="true">→</span> <ins>{after}</ins>
        </>
      )}
    </p>
  )
}

// A described feature drafted through /features/recipe. Recipes are single-catalog by API
// contract, so the source it was drafted against is snapshotted and registration lineage uses
// [snapshotSource, ref] pairs; the live source field may have changed since.
interface DraftCandidate {
  kind: 'draft'
  key: string
  name: string
  description: string
  recipe: Recipe
  snapshotSource: string
}

type Candidate = GeneratedCandidate | DraftCandidate

interface Registration {
  id: string
  freshness: FeatureFreshness | null
}

function specFor(candidate: Candidate): FeatureSpecIn {
  if (candidate.kind === 'generated') {
    const { idea } = candidate
    return {
      name: idea.name, description: idea.description, grain_table: idea.grain_table,
      aggregation: idea.aggregation, as_of_column: null,
      // Lineage comes from the pairs the backend resolved at recommend time, never from the
      // typed source context: the typed source can differ from where a derive actually lives.
      derives_from: idea.derives_pairs.map(([catalog_source, object_ref]) => ({
        catalog_source, object_ref })),
    }
  }
  const { recipe } = candidate
  return {
    name: candidate.name.trim(), description: candidate.description,
    grain_table: recipe.grain_table, aggregation: recipe.aggregation,
    as_of_column: recipe.as_of_column,
    // Recipes are single-catalog: every derive lives in the snapshotted source.
    derives_from: recipe.derives_from.map(object_ref => ({
      catalog_source: candidate.snapshotSource, object_ref })),
  }
}

function JoinPathDetails({ steps }: { steps: JoinStep[] }) {
  if (steps.length === 0) return null
  const fansOut = steps.some(s => stepFansOut(s.cardinality))
  const hasUnknownHop = steps.some(s => normalizeCardinality(s.cardinality) === null)
  return (
    <details>
      <summary style={{ cursor: 'pointer', padding: '6px 0', fontWeight: 500, color: 'var(--ink-soft)' }}>
        Join path ({steps.length} {steps.length === 1 ? 'hop' : 'hops'})
      </summary>
      <ol className="mono" style={{ margin: '8px 0 0', paddingLeft: 22, display: 'grid', gap: 4 }}>
        {steps.map(s => (
          <li key={`${s.from_ref}->${s.to_ref}`}>
            {s.from_ref} → {s.to_ref}{' '}
            <span
              style={
                stepFansOut(s.cardinality)
                  ? { color: 'var(--warn)', fontWeight: 600 }
                  : { color: 'var(--ink-soft)' }
              }
            >
              ({normalizeCardinality(s.cardinality) === null
                ? 'cardinality unknown'
                : s.cardinality})
            </span>
          </li>
        ))}
      </ol>
      {hasUnknownHop && (
        <p className="hint" style={{ marginTop: 8 }}>
          Cardinality is missing on at least one hop, so fan-out cannot be ruled out. Confirm the
          join direction before registering this feature.
        </p>
      )}
      {fansOut && (
        <div className="callout callout--warn">
          <CalloutGlyph d={WARN_GLYPH} />
          <div className="callout-body">
            <p>
              <strong>Fan-out.</strong> A one-to-many hop multiplies rows. Aggregate before
              joining or the feature will double-count.
            </p>
          </div>
        </div>
      )}
    </details>
  )
}

// Phase 2A: one recipe in the deterministic ranked eligible set — its canonical rank + recipe id, a
// "Why here" disclosure over the mapped `rank_reasons`, and (for a non-initial recipe) a SEPARATE
// "Why not shown initially" disclosure over `initial_view_reasons`. The two reason streams are kept
// visually distinct (two separately-labelled disclosures), never merged into one list.
function RankedRecipeRow({ recipe, warnings }: { recipe: RankedRecipe; warnings?: string[] }) {
  return (
    <li className="row" style={{ alignItems: 'flex-start', gap: 10 }}>
      <span className="micro-label tabular-nums" style={{ fontWeight: 600, marginTop: 6 }}>
        #{recipe.canonical_rank}
      </span>
      <div style={{ display: 'grid', gap: 6, flex: 1, minWidth: 0, padding: '6px 0' }}>
        <span className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <span className="mono" style={{ fontWeight: 600 }}>{recipe.recipe_id}</span>
          {/* Phase-2B SOFT-dimension warnings: presentation-only badges — never a rejection. */}
          {(warnings ?? []).map(code => (
            <span key={code} className="badge">{signalWarningText(code)}</span>
          ))}
        </span>
        <details className="rank-why">
          <summary style={{ cursor: 'pointer', color: 'var(--ink-soft)' }}>Why here</summary>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
            {recipe.rank_reasons.map(code => (
              <li key={code} style={{ color: 'var(--ink-soft)' }}>{rankReasonText(code)}</li>
            ))}
          </ul>
        </details>
        {/* Held-back recipes carry a DISTINCT stream: why they were not promoted into the initial
            view. Never merged with the rank reasons above. */}
        {!recipe.selected_for_initial_view && (
          <details className="rank-why-not">
            <summary style={{ cursor: 'pointer', color: 'var(--ink-soft)' }}>
              Why not shown initially
            </summary>
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {recipe.initial_view_reasons.map(code => (
                <li key={code} style={{ color: 'var(--ink-soft)' }}>
                  {initialViewReasonText(code)}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </li>
  )
}

// D3 1b — headings are FACTS about the feature (the closed operation-class vocabulary),
// never names of the prompt that produced it. Unlisted/absent classes read as conceptual.
const OPERATION_GROUPS: Record<string, string> = {
  sum: 'Flows & sums', count: 'Counts & frequency', distinct_count: 'Counts & frequency',
  ratio: 'Ratios & utilization', share: 'Ratios & utilization',
  recency: 'Recency & activity', extremum: 'Extremes & thresholds',
  dispersion: 'Volatility & dispersion', slope: 'Trends & slopes',
  snapshot: 'Point-in-time state', flag: 'Flags & indicators',
}

function operationGroup(c: Candidate): string {
  if (c.kind !== 'generated') return 'Drafts'
  const klass = c.idea.operation_class ?? ''
  return OPERATION_GROUPS[klass] ?? 'Conceptual patterns'
}

// ── Slice 2: narrowing the active set, never scoring it ─────────────────────────────────────────
//
// Every axis is metadata the SERVER already sent for THIS round: the per-option `binding_state`
// and the `blocked_actions` codes that ride with it, and the per-candidate `validation_status`.
// The client computes no quality score and no ranking of its own — "Recommended" stays the
// backend's advisory pick, with its caveat, beside the sets. These controls only HIDE rows: they
// never reorder them, and nothing here says one candidate is better than another.
//
// An axis whose value is unknown for a candidate is `null`, and a null never matches a filter on
// that axis. That is the honest arm rather than the convenient one: a free-form idea carries no
// binding state, and "did not come back review-not-current" is not the same claim as "reviewed".
const FACET_ORDER = ['binding', 'validation', 'review', 'tray'] as const
type FacetAxis = (typeof FACET_ORDER)[number]

const FACET_LEGEND: Record<FacetAxis, string> = {
  binding: 'Operand binding',
  validation: 'Design checks',
  review: 'Recipe review',
  tray: 'Your tray',
}

// What each facet is COUNTED FROM, said on the page. A filter whose provenance is invisible is
// indistinguishable from a filter the UI made up.
const FACET_SOURCE: Record<FacetAxis, string> = {
  binding: 'the binding state the engine returned per option',
  validation: 'the design-check status the engine returned per candidate',
  review: 'the engine’s own RECIPE_REVIEW_NOT_CURRENT blocker',
  tray: 'your current selection',
}

function facetOptionLabel(axis: FacetAxis, key: string): string {
  if (axis === 'binding') return BINDING_STATE_LABEL[key] ?? humanizeCode(key)
  if (axis === 'validation') {
    if (key === 'DESIGN_CHECKED') return 'Design-checked'
    if (key === 'NEEDS_EXTERNAL_VALIDATION') return 'Needs data checks'
    return humanizeCode(key)
  }
  if (axis === 'review') return 'Review not current'
  return 'Selected'
}

// One candidate's filterable values plus its search corpus. Computed once per render pass per
// candidate; `null` on an axis means "this round said nothing about that here".
interface CandidateFacts {
  search: string
  binding: string | null
  validation: string | null
  review: string | null
  tray: string | null
}

function candidateFacts(
  c: Candidate,
  entry: OptionActionsEntry | undefined,
  isSelected: boolean,
): CandidateFacts {
  const words = c.kind === 'generated'
    ? [c.idea.name, c.idea.description, c.idea.rationale, c.idea.aggregation ?? '',
      c.idea.grain_table ?? '', c.idea.recipe_id ?? '',
      ...c.idea.derives_from, ...c.idea.derives_pairs.map(pair => pair[1])]
    : [c.name, c.description, c.recipe.aggregation ?? '', c.recipe.grain_table ?? '',
      ...c.recipe.derives_from]
  // Review currency is its own axis and is only knowable for recipe-origin cards, exactly as the
  // row badge reads it — the same derivation, so a chip and a badge can never disagree.
  const reviewNotCurrent = c.kind === 'generated'
    && c.idea.generation_source === 'recipe'
    && (entry?.blocked_actions.create_contract ?? [])
      .some(blocker => blocker.code === 'RECIPE_REVIEW_NOT_CURRENT')
  return {
    search: words.join(' ').toLowerCase(),
    binding: entry?.binding_state ?? null,
    validation: (c.kind === 'generated' ? c.idea.validation_status : undefined) ?? null,
    review: reviewNotCurrent ? 'not_current' : null,
    tray: isSelected ? 'selected' : null,
  }
}

// Filter tokens are `axis:value`. Within one axis the chosen values are ORed (two binding states
// mean "either"); across axes they are ANDed (a binding state AND a design-check status).
function tokensByAxis(tokens: string[]): Map<FacetAxis, string[]> {
  const byAxis = new Map<FacetAxis, string[]>()
  for (const token of tokens) {
    const cut = token.indexOf(':')
    if (cut < 0) continue
    const axis = token.slice(0, cut) as FacetAxis
    if (!FACET_ORDER.includes(axis)) continue
    byAxis.set(axis, [...(byAxis.get(axis) ?? []), token.slice(cut + 1)])
  }
  return byAxis
}

function matchesAxes(
  facts: CandidateFacts,
  byAxis: Map<FacetAxis, string[]>,
  skip?: FacetAxis,
): boolean {
  for (const [axis, values] of byAxis) {
    if (axis === skip) continue
    const value = facts[axis]
    if (value === null || !values.includes(value)) return false
  }
  return true
}

interface FacetOption { token: string; label: string; count: number }
interface FacetGroup { axis: FacetAxis; legend: string; source: string; options: FacetOption[] }

// Facet options come from the values THIS active set actually carries — no authored menu, so an
// axis nobody measured simply has no controls. Each count is taken over the candidates that pass
// every OTHER axis, so the number on a chip is exactly what clicking it leaves on screen.
// A token the human has already chosen always keeps its control (even at count 0) — a filter you
// cannot see is a filter you cannot switch off.
function buildFacets(
  facts: CandidateFacts[],
  tokens: string[],
  query: string,
): FacetGroup[] {
  const byAxis = tokensByAxis(tokens)
  const groups: FacetGroup[] = []
  for (const axis of FACET_ORDER) {
    const counts = new Map<string, number>()
    for (const value of tokens
      .filter(token => token.startsWith(`${axis}:`))
      .map(token => token.slice(axis.length + 1))) {
      counts.set(value, 0)
    }
    for (const item of facts) {
      const value = item[axis]
      if (value === null) continue
      if (query !== '' && !item.search.includes(query)) continue
      if (!matchesAxes(item, byAxis, axis)) continue
      counts.set(value, (counts.get(value) ?? 0) + 1)
    }
    if (counts.size === 0) continue
    const options = [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([value, count]) => ({
        token: `${axis}:${value}`, label: facetOptionLabel(axis, value), count,
      }))
    groups.push({ axis, legend: FACET_LEGEND[axis], source: FACET_SOURCE[axis], options })
  }
  return groups
}

// ── Slice 2: the readiness ladder, from the STORED record's closed vocabulary ───────────────────
//
// BR-7's RECIPE_READINESS is an ordered ladder, so the record's `readiness` locates a definition
// on it exactly — no interpolation, no client-side scoring. It is deliberately NOT rendered on
// every row: readiness is not on the considered-set wire, only on the on-demand decision record,
// and inventing a rung for 900 rows from data nobody sent is the defect this codebase forbids.
//
// "Design check" is a rung of its own and is never the top one: a design-checked definition is
// still not a computed, back-tested feature.
const READINESS_LADDER: { key: string; label: string }[] = [
  { key: 'CONCEPTUAL_ONLY', label: 'Conceptual' },
  { key: 'FORMULA_BLOCKED', label: 'Formula blocked' },
  { key: 'FORMULA_AUTHORABLE', label: 'Formula authorable' },
  { key: 'FORMULA_VALIDATED', label: 'Formula validated' },
  { key: 'MATERIALIZATION_BLOCKED', label: 'Materialization blocked' },
  { key: 'MATERIALIZATION_READY', label: 'Materialization ready' },
]

// What each rung MEANS for the human, in the split this codebase enforces: nobody has decided
// yet / a data check is owed / structurally unsuitable — never a bare failure word.
const READINESS_MEANING: Record<string, string> = {
  CONCEPTUAL_ONLY: 'A pattern, with no executable definition authored yet.',
  FORMULA_BLOCKED: 'A formula cannot be authored until a named prerequisite is settled.',
  FORMULA_AUTHORABLE: 'A formula can be authored; nobody has authored one yet.',
  FORMULA_VALIDATED: 'The authored formula passed its checks.',
  MATERIALIZATION_BLOCKED: 'The formula stands; something named blocks computing it.',
  MATERIALIZATION_READY: 'Everything needed to compute this exists.',
  RETIRED: 'Withdrawn from the library; it will not be planned again.',
}

// D3 — the drawer body: everything rendered here is the STORED record verbatim; nothing is
// recomputed at read time. Sections: frozen roles + measured authorities, the losing
// shortlist (the candidates the engine considered and did not choose, with codes), the
// dataset story, the plan/PIT summary, the policy hashes, and the revision identity.
function AuditDrawerBody({ record }: { record: OptionDecisionRecord }) {
  const verdicts = record.evidence.verdicts ?? []
  const audit = record.evidence.eligibility_audit ?? []
  const bound = new Set(verdicts
    .filter(v => v.status === 'bound' && v.selected_ref)
    .map(v => `${String(v.role)}:${String(v.selected_ref)}`))
  const losing = audit.filter(e =>
    !bound.has(`${String(e.role)}:${String(e.object_ref)}`))
  const story = (record.dataset_story ?? {}) as Record<string, unknown>
  const plan = (story.binding_plan ?? null) as Record<string, unknown> | null
  const families = record.evidence.validation?.families ?? []
  // The ladder rung this record's readiness names. An unknown value from a newer backend (or
  // RETIRED, which is off the ladder) draws NO ladder — it renders as its own stated fact below.
  const rungIndex = READINESS_LADDER.findIndex(rung => rung.key === record.readiness)
  // Only the rows the story actually carries. A key the backend did not send is absent, never
  // filled with a plausible default — this pane is what WOULD be computed, so a guess here is
  // the most expensive kind of guess on the screen.
  const planRows: [string, string][] = plan
    ? ([
      ['Reads', plan.source_table],
      ['Population', plan.population_ref],
      ['Window', plan.window === undefined || plan.window === null
        ? null : `${String(plan.window)} days`],
      ['Point in time', plan.pit],
    ] as [string, unknown][])
      .filter((row): row is [string, string] =>
        row[1] !== null && row[1] !== undefined && String(row[1]) !== '')
      .map(([term, value]) => [term, String(value)])
    : []
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      {/* ── Readiness ─────────────────────────────────────────────────────────────────────────
          "Design-checked" and "can actually be computed" are different claims, and the platform
          stores them as different fields. Collapsing them is how a proposal starts reading like a
          deployable feature, so they get separate rungs and the blocker keeps its own sentence. */}
      <section aria-label="readiness">
        <h4 style={{ margin: '0 0 4px' }}>How far this definition has been taken</h4>
        {rungIndex >= 0 ? (
          <div
            className="ladder"
            role="img"
            aria-label={`Readiness: ${READINESS_LADDER[rungIndex].label.toLowerCase()}, `
              + `rung ${rungIndex + 1} of ${READINESS_LADDER.length}`}
          >
            {READINESS_LADDER.map((rung, i) => (
              <span
                key={rung.key}
                className="rung"
                data-state={i < rungIndex ? 'done' : i === rungIndex ? 'here' : 'todo'}
              >
                {rung.label}
              </span>
            ))}
          </div>
        ) : (
          <p style={{ margin: 0 }}>
            Readiness reported as <span className="mono">{record.readiness}</span>.
          </p>
        )}
        <p style={{ margin: '6px 0 0' }}>
          {READINESS_MEANING[record.readiness]
            ?? 'This readiness state is not one this screen has copy for; it is shown as the '
              + 'engine reported it.'}{' '}
          Readiness is separate from the design check
          {record.validation_status === 'NEEDS_EXTERNAL_VALIDATION'
            ? ', which is still owed data checks here'
            : ''}, and neither is a claim about predictive value.
        </p>
      </section>
      <section aria-label="frozen roles">
        <h4 style={{ margin: '0 0 4px' }}>Bound roles (frozen at serving)</h4>
        <ul style={{ margin: 0, paddingLeft: 16 }}>
          {verdicts.map((v, i) => (
            <li key={i}>
              <span style={{ fontWeight: 600 }}>{String(v.role)}</span>
              {' — '}{String(v.status)}
              {v.selected_ref ? <> · <span className="mono">{String(v.selected_ref)}</span></> : null}
              {Array.isArray(v.reason_codes) && v.reason_codes.length > 0
                ? <> · {v.reason_codes.join(', ')}</> : null}
            </li>
          ))}
        </ul>
      </section>
      {losing.length > 0 && (
        <section aria-label="losing shortlist">
          <h4 style={{ margin: '0 0 4px' }}>Considered and not chosen</h4>
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {losing.map((e, i) => (
              <li key={i}>
                <span className="mono">{String(e.object_ref)}</span>
                {' — '}{String(e.status)}
                {Array.isArray(e.reason_codes) && e.reason_codes.length > 0
                  ? <> · {e.reason_codes.join(', ')}</> : null}
              </li>
            ))}
          </ul>
        </section>
      )}
      {planRows.length > 0 && (
        // The frozen plan the engine already produced, as terms rather than prose: a reviewer
        // approving a definition should be able to read the read-set and the point-in-time rule
        // without leaving the page.
        <section aria-label="plan summary">
          <h4 style={{ margin: '0 0 4px' }}>Exactly what would be computed</h4>
          <dl className="plan-grid">
            {planRows.map(([term, value]) => (
              <Fragment key={term}>
                <dt>{term}</dt>
                <dd>{value}</dd>
              </Fragment>
            ))}
          </dl>
        </section>
      )}
      {families.length > 0 && (
        <section aria-label="policy families">
          <h4 style={{ margin: '0 0 4px' }}>Design checks by family</h4>
          <p style={{ margin: 0 }}>
            {families.map(f =>
              `${f.family}: ${f.state}${f.reason ? ` (${f.reason})` : ''}`).join(' · ')}
          </p>
        </section>
      )}
      <section aria-label="revision identity">
        <h4 style={{ margin: '0 0 4px' }}>Identity</h4>
        <p className="mono" style={{ margin: 0, fontSize: 12, overflowWrap: 'anywhere' }}>
          request {record.planning_request_hash.slice(0, 16)}… ·
          context {record.context_hash.slice(0, 16)}… ·
          matrix {(record.decision_manifest.authority_matrix_hash ?? '').slice(0, 16)}… ·
          recorded {record.recorded_at}
        </p>
      </section>
    </div>
  )
}


export function WorkbenchScreen() {
  const [goal, setGoal] = useState('')
  const [hypothesis, setHypothesis] = useState('')
  // The server-side intent that will later govern these candidates into a signed contract. Set
  // on a successful generate; dropped by clearSets on any invalidation (scope edit or error).
  const [intentId, setIntentId] = useState<string | null>(null)
  const [generationRunId, setGenerationRunId] = useState<string | null>(null)
  const [source, setSource] = useState('')
  const [target, setTarget] = useState('')
  // The mandatory read (intake build): the DRAFT ticket for the target confirm block, fetched
  // alongside recognition. Null = not fetched or unavailable — the manual target field is the
  // degrade path, so intake failing NEVER blocks the flow. `intakeReading` is the recorded HUMAN
  // answer (confirmed/corrected/exploring) once one lands.
  const [intake, setIntake] = useState<IntakeResp | null>(null)
  const [intakeReading, setIntakeReading] = useState<IntakeReading | null>(null)
  const [intakeCorrecting, setIntakeCorrecting] = useState(false)
  const [intakeCorrection, setIntakeCorrection] = useState('')
  const [intakeBusy, setIntakeBusy] = useState(false)
  const [intakeError, setIntakeError] = useState('')
  // T7 (c): the confirm gate's refusal, held so the human can read the SERVER's own words and
  // then act on them. `detail` is rendered verbatim and never summarised; `decision`/`ref` are
  // what the acknowledge control re-sends, so acknowledging cannot quietly retarget the answer.
  const [intakeAck, setIntakeAck] = useState<
    { detail: string; decision: 'confirmed' | 'corrected'; ref: string } | null>(null)
  // What the person actually acknowledged, kept after the signature so the record on screen is
  // the sentence they read — not a reconstruction of it.
  const [intakeAcknowledged, setIntakeAcknowledged] = useState('')
  const [generated, setGenerated] = useState<GeneratedCandidate[] | null>(null)
  // Ordered lenses of the last round's non-empty sets. Two or more render the compare cards;
  // one (or zero) renders the flat single list exactly as before the sets model.
  const [setLenses, setSetLenses] = useState<string[]>([])
  const [recommendation, setRecommendation] = useState<SetRecommendation | null>(null)
  // A4: option_id -> the server's action verdicts (same fold as the durable writes). Empty for
  // legacy/free-form cards until B1 retires them — those keep today's behavior, and drafting
  // them is still gated server-side.
  const [optionActions, setOptionActions] =
    useState<Record<string, OptionActionsEntry>>({})
  // D3 (UI-02) — the audit drawer: fetched ON DEMAND per option from the stored decision
  // record (never recomputed). Keyed by option id; null while loading, absent when closed.
  const [consideredRevisionId, setConsideredRevisionId] = useState<string | null>(null)
  const [auditOpenFor, setAuditOpenFor] = useState<string | null>(null)
  const [auditDetail, setAuditDetail] = useState<OptionDetailResp | null>(null)
  const [auditError, setAuditError] = useState('')
  const [rejections, setRejections] = useState<Rejection[]>([])
  const [rejectionsOpen, setRejectionsOpen] = useState(false)
  // T2's fourth outcome. `[]` is the honest empty lane; the response omitting the key entirely
  // (a v1/legacy body) reads the same on screen, because in both cases there is nothing to say.
  const [needsSetup, setNeedsSetup] = useState<NeedsSetupCandidate[]>([])
  // Open by default: on the arrangement this lane exists for it is the ENTIRE answer, and a
  // collapsed panel would reproduce the silence it was built to break.
  const [needsSetupOpen, setNeedsSetupOpen] = useState(true)
  // Which set's features the one detail list shows (multi-set rounds only).
  const [activeLens, setActiveLens] = useState<string | null>(null)
  // Slice 2: narrowing WITHIN the active set. Both are pure view state — they hide rows and
  // nothing else. Selection, registration and governance all read `allCandidates`, so a filtered
  // row keeps its pick and a hidden pick is still counted (and said) in the decision rail.
  const [candidateQuery, setCandidateQuery] = useState('')
  const [filterTokens, setFilterTokens] = useState<string[]>([])
  const [drafts, setDrafts] = useState<DraftCandidate[]>([])
  // GLOBAL selection across set views: candidate key -> the lens it was picked from (null for
  // drafts and flat-list picks). Keys carry the fetch sequence, so cleared rounds stay inert.
  const [selected, setSelected] = useState<Record<string, string | null>>({})
  // option_id -> the state of ITS formula draft, reported upward by each row's FormulaDraftAction.
  // Kept here and nowhere else so the tray can say "2 formulas ready · 2 require authoring" without
  // the tray knowing anything about drafting, and without a second reader asking the draft API.
  const [draftStates, setDraftStates] = useState<Record<string, FormulaDraftStatus['state']>>({})
  const onDraftStateChange = useCallback(
    (optionId: string, state: FormulaDraftStatus['state'] | null) => {
      setDraftStates(prev => {
        if (state === null) {
          if (!(optionId in prev)) return prev          // no draft: nothing to forget
          const { [optionId]: _gone, ...rest } = prev
          return rest
        }
        if (prev[optionId] === state) return prev       // same answer: no re-render
        return { ...prev, [optionId]: state }
      })
    }, [])
  const [registered, setRegistered] = useState<Record<string, Registration>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [screenedTarget, setScreenedTarget] = useState<string | null>(null)
  const [scopeChanged, setScopeChanged] = useState(false)
  const [describeOpen, setDescribeOpen] = useState(false)
  const [describeText, setDescribeText] = useState('')
  // Per-line draft failures, "Line N: <detail>", shown in the composer while the successful
  // lines still draft. Distinct from the top notice, which carries deployment-level facts.
  const [draftErrors, setDraftErrors] = useState<string[]>([])
  const [generating, setGenerating] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const [confirmingBatch, setConfirmingBatch] = useState(false)
  const [batchBusy, setBatchBusy] = useState(false)
  // Govern path, symmetric with register: which candidates minted a signed contract (contract
  // id + version), whether the tray is confirming a govern, and whether a govern batch is running.
  const [governed, setGoverned] = useState<Record<string, { contractId: string; version: number }>>({})
  const [confirmingGovern, setConfirmingGovern] = useState(false)
  const [governBusy, setGovernBusy] = useState(false)
  const [notice, setNotice] = useState('')
  // ---- Phase 1B: Gate #1 scope confirmation (behind intent_confirmation_ui) ----
  // The recognizer's attempt for the current round: non-null puts the screen in the confirm step
  // (proposed scope shown, no candidates yet), null means today's flow. The working scope the human
  // edits before confirming: the chosen primary use-case, the kept secondaries, and whether to
  // include descendant sub-use-cases (exact ↔ include_descendants).
  const [recognition, setRecognition] = useState<RecognitionResp | null>(null)
  const [recognitionTransition, setRecognitionTransition] =
    useState<RecognitionTransition | null>(null)
  const [scopePrimary, setScopePrimary] = useState<string | null>(null)
  const [scopeSecondary, setScopeSecondary] = useState<string[]>([])
  const [scopeExpansion, setScopeExpansion] = useState<'exact' | 'include_descendants'>('exact')
  // Phase-2B SOFT intent dimensions the human confirms/overrides at Gate #1: the confirmed governed
  // modelling contexts and the proposed prediction grain (target entity, null for none). Seeded from
  // the recognizer's proposal, editable, and threaded into BOTH confirm and broaden as ranking nudges
  // (never a scope-narrowing filter). `signalWarnings` is the scoped response's per-recipe warning map
  // (recipe_id -> codes), present only when the ranking flag is on; presentation-only, never a rejection.
  const [scopeContexts, setScopeContexts] = useState<string[]>([])
  const [scopeEntity, setScopeEntity] = useState<string | null>(null)
  // B10 — the unit-of-analysis confirmation: the server DERIVES a proposal from the signed
  // target's table grain; the human answers yes/no (or picks from the catalog's realistic
  // closed list). OPTIONAL by design: skipping it never blocks generation; confirming it makes
  // a wrong-grain candidate an actionable UOA_MISMATCH instead of a silently-served card.
  const [uoaProposal, setUoaProposal] = useState<UoaProposalResp | null>(null)
  const [uoaChoice, setUoaChoice] = useState<UoaOption | null>(null)
  const [uoaPicking, setUoaPicking] = useState(false)
  const [signalWarnings, setSignalWarnings] = useState<Record<string, string[]> | null>(null)
  // The scoped considered-set's per-recipe dispositions (the lens) and the scope id the last scoped
  // run was governed by — the prior scope a broaden supersedes. Both null on the unscoped path.
  const [dispositions, setDispositions] = useState<RecipeDisposition[] | null>(null)
  const [lastScopeId, setLastScopeId] = useState<string | null>(null)
  // Phase 2A: the deterministic presentation-priority ranking of the eligible recipes (+ its version
  // stamp), present only on a scoped response when the backend ranking flag is on (null otherwise).
  // `showAllRanked` toggles the initial-view "Show all" disclosure; it resets with each new round.
  const [ranking, setRanking] = useState<RankedRecipe[] | null>(null)
  const [rankingVersion, setRankingVersion] = useState<string | null>(null)
  const [showAllRanked, setShowAllRanked] = useState(false)
  // Whole-round feedback channel: the hypothesis and objective the round was generated for
  // (feedback reruns THOSE, not a since-edited input), the instruction being typed, rounds
  // consumed, the recorded strips, and the in-flight flag.
  const [roundHypothesis, setRoundHypothesis] = useState('')
  const [roundObjective, setRoundObjective] = useState('')
  // Slice 1: the compact submitted brief replaces the intake form once a round's snapshot exists.
  //
  // Slice 3 splits the two ways that form can be back on screen, because they are not the same
  // event and must not behave the same way:
  //
  //   `reviseOpen`    — the human CLICKED Revise brief. The brief moves into a drawer over the
  //                     still-live run. This is the only revise affordance anyone can choose.
  //   `briefRevising` — the form was ALREADY open (first round, or an error retry) and a round
  //                     landed while the caret was in it. Slice 1's protection: it stays exactly
  //                     where it is. Moving it into a drawer would remount the field mid-keystroke
  //                     and destroy the very revision the protection exists to keep.
  //
  // They are mutually exclusive by construction (see applyConsideredRound / recognize), so the
  // screen never shows two competing ways to revise the same brief.
  //
  // `roundToken` counts LANDED engine rounds and nothing else: it is the one signal the focus +
  // announcement effect fires on, so a selection, a registration or an approved refine never
  // re-announces a round.
  const [briefRevising, setBriefRevising] = useState(false)
  const [reviseOpen, setReviseOpen] = useState(false)
  const [roundToken, setRoundToken] = useState(0)
  const [liveMessage, setLiveMessage] = useState('')
  const [setFbInstruction, setSetFbInstruction] = useState('')
  const [setFbRounds, setSetFbRounds] = useState(0)
  const [setFbRecords, setSetFbRecords] = useState<SetFeedbackRecord[]>([])
  const [setFbBusy, setSetFbBusy] = useState(false)
  // Per-candidate refine channel, keyed by candidate key (cleared with its candidates).
  const [refines, setRefines] = useState<Record<string, RefineState>>({})
  // Out-of-order guards: only the latest request per handler may apply its response.
  const generateSeq = useRef(0)
  const draftSeq = useRef(0)
  // Reentry guard for the whole-round feedback call (one flight at a time, exactly once).
  const setFbInFlight = useRef(false)
  // Reentry guard per candidate for refine calls.
  const refineInFlight = useRef(new Set<string>())
  // Reentry guard for the draft batch: a second submit while a batch is in flight is a no-op,
  // so each line's recipe fires exactly once even before the disabled attribute lands.
  const draftInFlight = useRef(false)
  // Reentry guard for the register batch: state updates are async, so a double click on
  // Confirm approval could otherwise start two batches before the disabled attribute lands.
  const batchInFlight = useRef(false)
  // Reentry guard for the govern batch, mirroring batchInFlight: a double click on Confirm
  // govern must never mint two contracts for one candidate.
  const governInFlight = useRef(false)
  // Element id to focus after the next render (house pattern from ReviewQueueScreen): Approve
  // revision / Revert to original unmount the button that held focus, so without an explicit
  // move keyboard focus falls back to <body>.
  const focusTarget = useRef<string | null>(null)
  // The active stage's <h2>: focus moves here when a round lands, so a keyboard or screen-reader
  // user is put at the new work instead of left on the submit control they pressed.
  const stageHeadingRef = useRef<HTMLHeadingElement>(null)
  // Is the human's caret in one of the brief's TEXT fields right now? Read at the moment a round
  // lands, before React has touched the DOM: if they are mid-sentence the form must not collapse
  // under them and take the half-typed revision with it. Buttons inside the panel (Generate, the
  // path cards) deliberately do NOT count — clicking Generate is how a round starts.
  const briefTextFocused = useRef(false)
  // Latest-state mirrors (assigned every render) so async arrivals decide against CURRENT
  // state, not their submit-time closure: feedback pins read the selection as it stands when
  // the response lands, and a refine result checks whether its row registered meanwhile.
  const generatedRef = useRef(generated)
  generatedRef.current = generated
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const registeredRef = useRef(registered)
  registeredRef.current = registered
  const reviseOpenRef = useRef(reviseOpen)
  reviseOpenRef.current = reviseOpen

  // B10: derive the UOA proposal when a recognition lands (target + source known). A fetch
  // failure leaves the block absent — the confirmation is optional, absence is free.
  useEffect(() => {
    let cancelled = false
    setUoaProposal(null)
    setUoaChoice(null)
    setUoaPicking(false)
    const catalogSource = source.trim()
    if (!recognition || !catalogSource) return
    contractUoaProposal(catalogSource, {
      targetRef: target.trim() || undefined,
      recognizedEntity: scopeEntity ?? undefined,
    }).then(
      resp => { if (!cancelled) setUoaProposal(resp) },
      () => { /* optional: absence never blocks */ },
    )
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recognition?.recognition_id])

  useEffect(() => {
    if (!focusTarget.current) return
    const el = document.getElementById(focusTarget.current)
    focusTarget.current = null
    el?.focus()
  })

  // Phase 1B flags, read each render so a test can flip them with vi.stubEnv. Default OFF → today.
  const confirmationUi = confirmationUiEnabled()
  const dispositionLens = dispositionLensEnabled()
  const rankingUi = rankingEnabled()
  // Phase 2A: the ranked eligible set for render — the initial-view subset first, the rest behind
  // "Show all". The backend already emits a dense canonical order, but sort defensively by
  // canonical_rank so the projection is stable regardless of the response array's order.
  const rankedOrder = ranking !== null
    ? [...ranking].sort((a, b) => a.canonical_rank - b.canonical_rank)
    : []
  const rankedInitial = rankedOrder.filter(r => r.selected_for_initial_view)
  const rankedRest = rankedOrder.filter(r => !r.selected_for_initial_view)
  // The recognizer's proposals as the working scope holds them: the chosen primary and the kept
  // secondaries, resolved back to their candidate records for display (name/confidence/evidence).
  const primaryCandidate =
    recognition?.candidates.find(c => c.use_case_id === scopePrimary) ?? null
  const secondaryCandidates: RecognitionCandidate[] = scopeSecondary
    .map(id => recognition?.candidates.find(c => c.use_case_id === id))
    .filter((c): c is RecognitionCandidate => c != null)

  const multiSet = setLenses.length > 1
  // Every live candidate, across ALL sets plus drafts: selection and registration always work
  // over this, so picks survive set switching and a batch registers whatever is selected even
  // when another set's view is showing.
  const allCandidates: Candidate[] = [...(generated ?? []), ...drafts]
  // What the one detail list shows: the active set's features (multi-set rounds) or every
  // generated candidate (flat rounds), plus drafts in both cases. Kept pins belong to no new
  // set, so they show in every view.
  const visibleGenerated = multiSet && activeLens !== null
    ? (generated ?? []).filter(c => c.kept === true || c.lenses.includes(activeLens))
    : generated ?? []
  const listCandidates: Candidate[] = [...visibleGenerated, ...drafts]
  // Slice 2: search + facets narrow WITHIN the active set. The facts come from the wire (per
  // option `binding_state` and its blocker codes, per candidate `validation_status`) plus the
  // human's own tray; the client adds no score and no reordering. `readiness` is NOT here on
  // purpose: it is not on the considered-set response — only on the on-demand decision record —
  // so a readiness facet over this list would be counting a field nobody sent.
  const activeQuery = candidateQuery.trim().toLowerCase()
  const listFacts = new Map<string, CandidateFacts>()
  for (const c of listCandidates) {
    const entry = c.kind === 'generated' && c.idea.option_id
      ? optionActions[c.idea.option_id]
      : undefined
    listFacts.set(c.key, candidateFacts(c, entry, c.key in selected))
  }
  const activeAxes = tokensByAxis(filterTokens)
  const facetGroups = buildFacets([...listFacts.values()], filterTokens, activeQuery)
  const filteredList = listCandidates.filter(c => {
    const facts = listFacts.get(c.key)!
    if (activeQuery !== '' && !facts.search.includes(activeQuery)) return false
    return matchesAxes(facts, activeAxes)
  })
  const narrowing = activeQuery !== '' || filterTokens.length > 0
  // Picks the current narrowing hides. Selection is global by design, so the rail must say when
  // its count includes rows the list is not showing — a consequence you cannot see is the exact
  // thing the decision rail exists to prevent.
  const hiddenSelectedCount = narrowing
    ? listCandidates.filter(c => c.key in selected && !filteredList.includes(c)).length
    : 0
  // D3 1b: group the browsing list by each candidate's TYPED operation class. Only when at
  // least two REAL groups exist (else the flat list stays byte-identical); order = first
  // appearance, so the engine's ranking still decides what leads.
  const groupOrder: string[] = []
  for (const c of filteredList) {
    const g = operationGroup(c)
    if (!groupOrder.includes(g)) groupOrder.push(g)
  }
  const groupedList: Array<{ heading: string | null; candidate: Candidate }> = []
  if (groupOrder.length > 1) {
    for (const g of groupOrder) {
      let first = true
      for (const c of filteredList.filter(x => operationGroup(x) === g)) {
        groupedList.push({ heading: first ? g : null, candidate: c })
        first = false
      }
    }
  } else {
    for (const c of filteredList) groupedList.push({ heading: null, candidate: c })
  }
  // One definition per non-empty line: the button label and its gating read this directly.
  const draftLines = describeText.split('\n').map(line => line.trim()).filter(Boolean)
  // Only generated candidates pass the design gauntlet, so the design-checked explanation
  // appears only when the list holds at least one generated candidate.
  const hasGenerated = (generated?.length ?? 0) > 0
  // …and only while one of them actually WEARS that stamp. Since T3 the server derives
  // `verification` from the recipe's readiness as well as the gauntlet's verdict, so a
  // generated card legitimately reads UNVERIFIED — and on today's registry that is almost all
  // of them (3 of 317 recipes can earn DESIGN-CHECKED). Gating the sentence on "there are
  // candidates" would leave the page explaining a badge none of its cards carry, which is the
  // page-level version of the badge lie T3 removed from the cards.
  const hasDesignChecked = (generated ?? []).some(
    c => c.idea.verification === 'DESIGN-CHECKED')
  // Selection is the intersection of the map and the live candidate list: keys from cleared
  // rounds are inert, and registered candidates can never re-enter a batch.
  const selectedCandidates = allCandidates.filter(
    c => c.key in selected && !registered[c.key] && !governed[c.key])
  const selectedCount = selectedCandidates.length
  // "2 formulas ready · 1 blocked · 1 needs authoring" — what the selection would cost to prepare,
  // said before anyone presses anything. Derived from the rows' reported draft states rather than
  // fetched, so building this sentence spends nothing.
  //
  // NEEDS AUTHORING counts what has no draft AND what did not finish — a FAILED draft needs
  // authoring again, and folding it into "ready" would promise a formula that is not there.
  // BLOCKED is counted separately because it is neither: the formula exists and cannot run here,
  // which is a different remedy from asking for one.
  const draftSummary: string | null = (() => {
    if (selectedCount === 0) return null
    let ready = 0
    let blocked = 0
    for (const candidate of selectedCandidates) {
      const optionId = candidate.kind === 'generated' ? candidate.idea.option_id : null
      const state = optionId ? draftStates[optionId] : undefined
      if (state === 'READY') ready += 1
      else if (state === 'BLOCKED') blocked += 1
    }
    const needsAuthoring = selectedCount - ready - blocked
    const parts = [`${ready} formula${ready === 1 ? '' : 's'} ready`]
    if (blocked > 0) parts.push(`${blocked} blocked`)
    if (needsAuthoring > 0) parts.push(`${needsAuthoring} need${needsAuthoring === 1 ? 's' : ''} authoring`)
    return parts.join(' · ')
  })()
  // Only fresh, unrefined generated candidates are governable: they came through the CURRENT intent's
  // considered set AND still match its persisted snapshot. A draft is the human's own definition; a
  // KEPT candidate was pinned from a PRIOR generation; and a REFINED candidate's idea was mutated in
  // place (approveRevision) so it no longer matches the snapshot the server reconstructs the choice
  // from — governing it would 422 or silently mint a contract from the pre-refine data. None govern.
  const governableCount = selectedCandidates.filter(
    c => c.kind === 'generated' && !c.kept && (refines[c.key]?.appliedRound ?? null) === null).length
  // Per-item outcomes for the rail, counted over the round rather than over the visible list:
  // a row that scrolled away, or that the current filter hides, still had its write attempted.
  const settledRows = allCandidates.filter(
    c => registered[c.key] !== undefined || governed[c.key] !== undefined).length
  const failedRows = allCandidates.filter(c => errors[c.key] !== undefined).length
  // Distinct set origins of the current picks, for the tray's mix note.
  const originLenses = [...new Set(
    selectedCandidates
      .map(c => selected[c.key])
      .filter((lens): lens is string => typeof lens === 'string'),
  )]

  // The one mutual-exclusion rule between feedback and registration: while the tray is
  // confirming approval or a register batch is in flight, both feedback channels disable,
  // Approve revision / Revert to original are inert, the generate path locks, and scope edits
  // are inert. A revision must never race what the human is about to write into the registry,
  // and a registration must never complete invisibly after its row leaves view.
  const feedbackLocked = confirmingBatch || batchBusy || confirmingGovern || governBusy
  const setFbExhausted = setFbRounds >= FEEDBACK_ROUNDS

  // Gates advance with real state, never decoratively. The brief needs BOTH the hypothesis and the
  // goal (generate() requires both), so the gate must not flip to 'done' — and cascade gate2 to
  // 'active' — while a required field is still empty.
  const goalDone = goal.trim() !== '' && hypothesis.trim() !== ''
  const haveCandidates = allCandidates.length > 0
  const anyRegistered = allCandidates.some(c => registered[c.key] !== undefined)
  const gate1: GateState = goalDone ? 'done' : 'active'
  const gate2: GateState = haveCandidates ? 'done' : goalDone ? 'active' : 'todo'
  const gate3: GateState = !haveCandidates
    ? 'todo'
    : selectedCount > 0 || anyRegistered ? 'done' : 'active'
  const gate4: GateState = selectedCount > 0 ? 'active' : anyRegistered ? 'done' : 'todo'

  // ---- Slice 1: the derived view phase, and the shell that renders from it ----
  // A brief has been SUBMITTED once the round snapshot exists — the exact text the engine was
  // given. That snapshot is written when a round lands (recognition, or a considered set) and
  // dropped by any scope edit that voids the round, so its presence and the results on screen can
  // never disagree. The draft `hypothesis` / `goal` fields deliberately do NOT feed this: they are
  // the next brief, not this one.
  const briefSubmitted = roundHypothesis.trim() !== ''
  const phase = derivePhase({
    awaitingScope: confirmationUi && recognition !== null,
    generating,
    candidateCount: allCandidates.length,
    roundReturnedNothing: generated !== null && generated.length === 0,
    selectedCount,
    settledCount: allCandidates.filter(
      c => registered[c.key] !== undefined || governed[c.key] !== undefined).length,
    hasNotice: notice !== '',
  })
  // The full intake form belongs to the draft shell. It comes back for exactly three reasons: no
  // brief has been submitted yet, the human asked to revise, or the run failed and the fastest
  // honest recovery is the form they already filled in (no snapshot describes a failed run, so
  // there is nothing to collapse to).
  // Slice 3: the drawer needs a LIVE run behind it — that is the whole point of it being a drawer
  // rather than the page. A failed run has no results to preserve and no snapshot worth quoting,
  // so `error` keeps Slice 1's inline retry form instead.
  const reviseDrawerOpen = reviseOpen && briefSubmitted && phase !== 'error'
  const formOpen = !briefSubmitted || briefRevising || reviseDrawerOpen || phase === 'error'
  // Have the fields drifted from the brief the visible run was generated from? After a landed
  // round they are equal by construction (the snapshot IS what was submitted), so this is true
  // only when the human has typed something they have not generated yet.
  const briefDiffersFromSnapshot = briefSubmitted
    && (hypothesis.trim() !== roundHypothesis.trim() || goal.trim() !== roundObjective.trim())
  const stage = STAGE_COPY[phase]
  // What this run's options are made of, counted from the wire. Empty for legacy/free-form rounds
  // that carry no option-actions entries — then the strip does not render at all rather than
  // implying a composition nobody measured.
  const optionEntries = Object.values(optionActions)
  const composition = composeBindingStates(optionEntries)

  // Generation completion is a deliberate transition: focus moves to the active stage heading so a
  // keyboard user lands on the new work, and the count change is announced politely. Fires on
  // `roundToken` alone — one event per LANDED round, never on a selection or a re-render. Focus is
  // NOT stolen while the human is typing (the review's constraint: no forced scroll mid-keystroke);
  // the announcement still goes out, because that costs the typist nothing.
  useEffect(() => {
    if (roundToken === 0) return
    const count = generated?.length ?? 0
    const sets = setLenses.length
    setLiveMessage(
      count === 0
        ? 'No candidates were returned for this brief.'
        : `Results ready: ${sets} ${sets === 1 ? 'set' : 'sets'}, `
          + `${count} ${count === 1 ? 'candidate' : 'candidates'}.`)
    const active = document.activeElement
    const typing = active instanceof HTMLElement
      && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)
    if (!typing) stageHeadingRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roundToken])

  // Slice 3: opening the drawer moves the caret into the first field it holds, so a keyboard user
  // lands in the edit rather than being left on a control that has just disappeared. Fires on the
  // OPEN transition only — never on a re-render, and never while a round is landing (that path
  // goes through settleReviseSurfaces, which keeps the caret where it already is).
  useEffect(() => {
    if (!reviseDrawerOpen) return
    document.getElementById('wb-hypothesis')?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviseDrawerOpen])

  // THE SCREEN SAYS WHAT THE SERVER SAID. No status code gets its own sentence here — a 503 used
  // to be rewritten as "no LLM provider is enabled", which cost an owner a diagnosis on
  // 2026-08-24: the real `detail` named a governance interlock, and the screen sent them to look
  // at provider configuration instead. A status word is a class of failure, never its cause, and
  // the response's own sentence is the only thing on the wire that knows which one this was.
  // `ApiError.detail` is never blank (the transport falls back to statusText, then `HTTP <n>`),
  // so there is no empty-banner case to compose around.
  function fail(err: unknown) {
    setNotice(err instanceof ApiError ? err.detail : String(err))
  }

  // Slice 2: the search box and the facet chips are scoped to ONE round's active set. A new
  // round replaces that set, so carrying a filter across would hide fresh candidates behind a
  // control the human aimed at candidates that no longer exist. Cleared with every path that
  // replaces or voids the round.
  function clearResultView() {
    setCandidateQuery('')
    setFilterTokens([])
  }

  function clearSets() {
    clearResultView()
    setSetLenses([])
    setRecommendation(null)
    setActiveLens(null)
    setRejections([])
    setNeedsSetup([])
    setRejectionsOpen(false)
    // Drop any stale governance intent: the candidates it governed no longer exist.
    setIntentId(null)
    setGenerationRunId(null)
  }

  // A scope edit voids the round, so it voids the round's IDENTITY too: the submitted brief
  // described a run whose candidates no longer exist, and leaving the card up would be the same
  // "results beside text that did not produce them" defect this slice exists to remove. Dropping
  // the snapshot returns the screen to its draft shell, which is exactly what the human now needs.
  // Not folded into clearFeedback(): applyConsideredRound calls that AFTER writing the snapshot.
  function clearRoundBrief() {
    setRoundHypothesis('')
    setRoundObjective('')
    setBriefRevising(false)
    // No run left to revise: the drawer's whole premise (a live round behind it) is gone, so it
    // closes and the draft shell takes over.
    setReviseOpen(false)
  }

  // Slice 3, the Cancel arm: return to the current run unchanged. It writes NOTHING — not the
  // fields, not the scope, not the round — so cancelling is always free. The human's in-progress
  // text is deliberately kept (re-opening finds it, with the "these are your edits" note beside
  // it); silently reverting their typing would be its own kind of invalidation.
  function closeRevise() {
    setReviseOpen(false)
    // The trigger is UNMOUNTED while the drawer is open, so a ref would be null here. The
    // house pattern (focus an element id after the next render) puts the caret back on the
    // control the human opened the drawer from, instead of dropping it on <body>.
    focusTarget.current = 'wb-revise-open'
  }

  // Slice 3: a round has landed and the brief on screen is authoritative again, so whichever
  // revise surface is open closes — UNLESS the human is mid-keystroke in it, in which case
  // closing would delete the revision they are writing (Slice 1's protection, now applied to
  // both surfaces). The two are set as an either/or so a drawer's content is never handed to the
  // inline form, or the reverse, which would remount the field under the caret.
  function settleReviseSurfaces() {
    const typing = briefTextFocused.current
    if (reviseOpenRef.current) {
      setReviseOpen(typing)
      setBriefRevising(false)
      return
    }
    setBriefRevising(typing)
  }

  // Resets both feedback channels: round counters, recorded strips, typed instructions, and
  // every per-candidate refine state. Runs with every path that replaces or clears the round.
  function clearFeedback() {
    setSetFbRounds(0)
    setSetFbRecords([])
    setSetFbInstruction('')
    setSetFbBusy(false)
    setRefines({})
  }

  // A scope edit voids any in-flight round (generate or whole-round feedback): bump the
  // sequence so a late response is discarded instead of applying against the new scope, and
  // release the busy flags the discarded flights can no longer clear.
  function voidInFlightRounds() {
    generateSeq.current += 1
    setGenerating(false)
    // A scope edit also drops the Gate #1 confirm step and any scoped disposition lens: the
    // recognised scope was for the previous scope context. (Both no-ops when the flags are off.)
    setRecognition(null)
    setRecognitionTransition(null)
    setDispositions(null)
    // The deterministic ranking was for the previous scope too; drop it (no-op when the flag is off).
    setRanking(null)
    // The per-recipe SOFT-dimension warnings belonged to that ranking too (no-op when the flag is off).
    setSignalWarnings(null)
  }

  function changeSource(value: string) {
    setSource(value)
    // Generated candidates were produced for the previous source context, and draft snapshots
    // no longer match it either: a source edit clears everything.
    const hadCandidates = allCandidates.length > 0
    voidInFlightRounds()
    setGenerated(null)
    setDrafts([])
    setSelected({})
    setRegistered({})
    setGoverned({})
    setErrors({})
    setDraftErrors([])
    setScreenedTarget(null)
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    clearSets()
    clearFeedback()
    clearRoundBrief()
    if (hadCandidates) setScopeChanged(true)
  }

  // A target edit invalidates generated candidates and their round's sets and rejections (they
  // were gathered and screened for the previous scope). Drafts survive: their snapshot source is
  // unchanged.
  function invalidateGenerated() {
    const hadGenerated = (generated?.length ?? 0) > 0
    voidInFlightRounds()
    setGenerated(null)
    setSelected({})
    setScreenedTarget(null)
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    clearSets()
    clearFeedback()
    clearRoundBrief()
    if (hadGenerated) setScopeChanged(true)
  }

  function changeTarget(value: string) {
    setTarget(value)
    invalidateGenerated()
  }

  // Apply a considered-set response as the current round. Every opaque option remains selectable even
  // when several variants share a display name. Shared by legacy and confirmed-scope generation.
  function applyConsideredRound(
    cs: ConsideredSetResp, seq: number, roundHyp: string, roundObj: string,
    resetFeedback = true,
  ) {
    setIntentId(cs.intent_id)
    setGenerationRunId(cs.generation_run_id ?? null)
    const candidates: GeneratedCandidate[] = []
    const lenses: string[] = []
    for (const [setIndex, set] of cs.alternatives.entries()) {
      if (set.features.length === 0) continue
      lenses.push(set.lens)
      for (const [featureIndex, idea] of set.features.entries()) {
        candidates.push({
          kind: 'generated',
          key: `g${seq}:${idea.option_id ?? `${setIndex}:${featureIndex}:${idea.name}`}`,
          idea,
          lenses: [set.lens],
        })
      }
    }
    setGenerated(candidates)
    setSetLenses(lenses)
    setRecommendation(cs.recommendation)
    setOptionActions(Object.fromEntries(
      [...(cs.recommended_options ?? []), ...(cs.actionable_options ?? [])]
        .map(entry => [entry.option_id, entry])))
    setConsideredRevisionId(cs.considered_revision_id ?? null)
    setAuditOpenFor(null)
    setAuditDetail(null)
    setAuditError('')
    // The detail list opens on the advisory pick when there is one among the surviving sets.
    setActiveLens(
      lenses.length > 1
        ? cs.recommendation !== null && lenses.includes(cs.recommendation.recommended_lens)
          ? cs.recommendation.recommended_lens
          : lenses[0]
        : null)
    setRejections(cs.rejections)
    // T2: the fourth outcome rides the same round reset as the other three. A v1/legacy body
    // omits the key; `[]` there is 'nothing to show', which is what an absent key means too.
    setNeedsSetup(cs.needs_setup ?? [])
    setRejectionsOpen(false)
    clearResultView()
    setScreenedTarget(target.trim() || null)
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    // Phase 1B: carry the scoped disposition lens + the governing scope id a later broaden
    // supersedes. Both null on the unscoped/one-shot path (the response omits them).
    setDispositions(cs.dispositions ?? null)
    setLastScopeId(cs.scope_id ?? null)
    // Phase 2A: carry the deterministic ranking (+ version) when the backend put it on the response;
    // reset the "Show all" disclosure for the new round. Absent → null (ranking flag off / unscoped).
    setRanking(cs.ranking ?? null)
    setRankingVersion(cs.ranking_version ?? null)
    // Phase 2B: the per-recipe SOFT-dimension warning map, present only with the ranking flag on
    // (absent → null). Presentation-only badges — never a rejection or a disposition change.
    setSignalWarnings(cs.signal_warnings ?? null)
    setShowAllRanked(false)
    // A fresh engine round starts a fresh feedback cycle against ITS hypothesis and objective:
    // whole-round feedback reruns these even if the inputs are edited later.
    setRoundHypothesis(roundHyp)
    setRoundObjective(roundObj)
    // The round landed: the submitted brief is authoritative again, so whichever revise surface
    // is open closes — UNLESS the caret is in it, in which case collapsing would delete a
    // revision the human is in the middle of writing.
    settleReviseSurfaces()
    setRoundToken(token => token + 1)
    if (resetFeedback) clearFeedback()
  }

  function finishFeedbackTransition(
    transition: RecognitionTransition,
    prior: GeneratedCandidate[],
  ) {
    const pinned = prior.filter(candidate =>
      candidate.key in selectedRef.current
      || registeredRef.current[candidate.key] !== undefined)
    const pinnedKeys = new Set(pinned.map(candidate => candidate.key))
    const keptSelected = pinned
      .filter(candidate => registeredRef.current[candidate.key] === undefined).length
    setGenerated(fresh => [
      ...pinned.map((candidate): GeneratedCandidate => ({
        ...candidate, kept: true, lenses: [],
      })),
      ...(fresh ?? []),
    ])
    setSelected(previous => Object.fromEntries(
      Object.entries(previous).map(([key, origin]) =>
        pinnedKeys.has(key) ? [key, null] : [key, origin])))
    setSetFbRounds(round => round + 1)
    setSetFbRecords(records => [...records, {
      round: records.length + 1,
      user: getSession().user,
      instruction: transition.feedback,
      kept: keptSelected,
      replaced: prior.length - pinned.length,
    }])
    setSetFbInstruction('')
    setRefines(previous => Object.fromEntries(
      Object.entries(previous).filter(([key]) => pinnedKeys.has(key))))
  }

  // Phase 1B (intent_confirmation_ui): recognise the objective and enter the confirm step. NO
  // considered-set call yet — the human confirms/overrides/broadens the proposed scope first.
  // Fail-open: the endpoint never 5xxs, so a technical failure still lands as a recognition here.
  async function recognize(objective: string) {
    const seq = ++generateSeq.current
    setNotice('')
    setScopeChanged(false)
    setGenerating(true)
    // The confirm step shows only the proposed scope: clear any prior round's candidates + lens.
    setGenerated(null)
    setDispositions(null)
    setRanking(null)
    setSignalWarnings(null)
    clearSets()
    clearFeedback()
    // The mandatory read runs ALONGSIDE recognition (one cached call — a repeat hypothesis is
    // free). Degrade-never-block, on the UI too: an intake failure (older backend, no LLM) leaves
    // `intake` null and the manual target field carries the flow exactly as before.
    setIntake(null)
    setIntakeReading(null)
    setIntakeCorrecting(false)
    setIntakeError('')
    setIntakeAck(null)
    setIntakeAcknowledged('')
    const intakeSeq = seq
    contractIntake(hypothesis.trim(), { catalogSource: source.trim() || undefined })
      .then(resp => {
        if (intakeSeq !== generateSeq.current) return
        setIntake(resp)
        // A pinned name is already recorded server-side (user_typed, no click needed): thread it
        // into the manual field so the considered-set request carries what the server signed.
        if (resp.ticket.pinned && resp.ticket.target_column) setTarget(resp.ticket.target_column)
      })
      .catch(() => { /* degrade to the manual target field */ })
    try {
      const rec = await contractRecognitions(hypothesis.trim(), objective)
      if (seq !== generateSeq.current) return
      setRoundHypothesis(hypothesis.trim())
      setRoundObjective(objective)
      // The snapshot is taken: from here the brief is what the RUN was submitted with, and a
      // re-opened draft form closes back down to it (unless the human is mid-keystroke in it).
      settleReviseSurfaces()
      setRecognition(rec)
      setRecognitionTransition(null)
      setScopePrimary(rec.candidates.find(c => c.relationship === 'primary')?.use_case_id ?? null)
      setScopeSecondary(
        rec.candidates.filter(c => c.relationship === 'secondary').map(c => c.use_case_id))
      setScopeExpansion('exact')
      // Phase-2B: seed the SOFT dimensions from the recognizer's proposal — the human confirms or
      // overrides them below before confirm/broaden. Empty/null when the recognizer proposed none.
      setScopeContexts(rec.modelling_contexts)
      setScopeEntity(rec.target_entity)
    } catch (err) {
      if (seq !== generateSeq.current) return
      setRecognition(null)
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  // Record the human's answer to the target confirm block — the provenance flip to a person.
  // 'confirmed' signs the draft as shown; 'corrected' signs the ref the human typed instead (the
  // click is the extractor's ground-truth telemetry); 'exploring' records an explicit no-target
  // declaration. The signed value is threaded into the manual target field so the considered-set
  // request and the server's record agree.
  //
  // T7 (c): `acknowledge` is the PERSON's, so it is sent only from the control the refusal
  // banner puts under the server's own sentence — never by default, and never on the first
  // attempt. That first attempt is what earns the sentence; sending the flag ahead of it would be
  // exactly the undisclosed commit this gate exists to stop.
  async function answerIntake(
    decision: 'confirmed' | 'corrected' | 'exploring',
    ref?: string,
    acknowledge = false,
  ) {
    if (!intake || intakeBusy) return
    setIntakeBusy(true)
    setIntakeError('')
    try {
      const t = intake.ticket
      const reading = await contractIntakeTarget(intake.intent_id, decision, {
        targetRef: decision === 'exploring' ? undefined : ref,
        targetWindowDays: t.target_window_days ?? undefined,
        targetType: t.target_type !== 'abstain' ? t.target_type : undefined,
        businessDomain: t.business_domain,
        catalogSource: source.trim() || undefined,
        targetNotOutcomeAcknowledged: acknowledge,
      })
      setIntakeReading(reading)
      // Keep the sentence the person actually read, so the signed block records THAT rather than
      // a version of it rebuilt from the class afterwards.
      setIntakeAcknowledged(acknowledge ? (intakeAck?.detail ?? '') : '')
      setIntakeAck(null)
      setIntakeCorrecting(false)
      setIntakeCorrection('')
      setTarget(reading.target_ref ?? '')
    } catch (err) {
      // The confirm gate's own refusal: hold the SERVER's sentence and offer the acknowledgment
      // beside it. Anything else stays an error line — including a 422 this screen cannot
      // identify, which must never quietly become an offer to acknowledge.
      if (err instanceof ApiError && err.status === 422 && decision !== 'exploring' && ref
        && err.detail.includes(NOT_OUTCOME_ACK_FIELD)) {
        setIntakeAck({ detail: err.detail, decision, ref })
      } else {
        setIntakeError(err instanceof ApiError
          ? err.detail
          : 'Could not record the target decision. Try again.')
      }
    } finally {
      setIntakeBusy(false)
    }
  }

  // D3: open/close the audit drawer for one option — the stored decision record on demand.
  async function toggleAudit(optionId: string) {
    if (auditOpenFor === optionId) {
      setAuditOpenFor(null)
      setAuditDetail(null)
      return
    }
    if (!consideredRevisionId) return
    setAuditOpenFor(optionId)
    setAuditDetail(null)
    setAuditError('')
    try {
      setAuditDetail(await contractOptionDetail(consideredRevisionId, optionId))
    } catch (err) {
      setAuditError(err instanceof ApiError
        ? err.detail
        : 'Could not load the stored decision record.')
    }
  }

  // The human confirmed the recognised scope: mint the run, persist the scope, and ground only the
  // in-scope subset. Reuses the round's snapshotted hypothesis/objective (set at recognise time).
  async function confirmScope() {
    const rec = recognition
    if (!rec || feedbackLocked) return
    const transition = recognitionTransition
    const prior = generatedRef.current ?? []
    const seq = ++generateSeq.current
    setNotice('')
    setGenerating(true)
    try {
      const cs = await contractConsideredSet(roundHypothesis, roundObjective, {
        catalogSource: source.trim() || undefined,
        targetRef: target.trim() || undefined,
        intentId: rec.intent_id,
        recognitionId: rec.recognition_id,
        feedback: transition?.feedback,
        confirmedScope: {
          primary: scopePrimary,
          secondary: scopeSecondary,
          expansion: scopeExpansion,
          unscoped: false,
          // SOFT dimensions the human confirmed/overrode: ranking nudges only, never a scope filter.
          modellingContexts: scopeContexts,
          targetEntity: scopeEntity,
          uoaEntity: uoaChoice?.entity ?? null,
          spineRef: uoaChoice?.spine_ref ?? null,
        },
        supersedesScopeId: transition?.supersedesScopeId,
      })
      if (seq !== generateSeq.current) return
      applyConsideredRound(
        cs, seq, roundHypothesis, roundObjective, transition === null)
      if (transition) {
        finishFeedbackTransition(transition, prior)
      }
      setRecognition(null)
      setRecognitionTransition(null)
    } catch (err) {
      if (seq !== generateSeq.current) return
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  // Broaden ("show all buildable recipes"): re-run UNSCOPED under a fresh run, superseding the prior
  // scope (lineage only). Available from the proposed-scope panel and from the disposition lens.
  async function broadenScope() {
    if (feedbackLocked) return
    const rec = recognition
    const transition = recognitionTransition
    const prior = generatedRef.current ?? []
    const seq = ++generateSeq.current
    setNotice('')
    setGenerating(true)
    try {
      const cs = await contractConsideredSet(roundHypothesis, roundObjective, {
        catalogSource: source.trim() || undefined,
        targetRef: target.trim() || undefined,
        // Prefer the committed intentId: from the disposition-lens broaden (~post-generation) `rec` is
        // already cleared, so falling back to `rec?.intent_id` alone would mint a FRESH intent and orphan
        // the run/scope lineage. `intentId` holds the confirmed round's intent (set by applyConsideredRound).
        intentId: intentId ?? rec?.intent_id,
        recognitionId: rec?.recognition_id,
        feedback: transition?.feedback,
        confirmedScope: {
          primary: null,
          secondary: [],
          expansion: 'exact',
          unscoped: true,
          // Dimensions are SOFT ranking nudges that still apply to the broadened (unscoped) set.
          modellingContexts: scopeContexts,
          targetEntity: scopeEntity,
          // Confirmed DATA orthogonal to use-case scoping — a broaden does not forget it.
          uoaEntity: uoaChoice?.entity ?? null,
          spineRef: uoaChoice?.spine_ref ?? null,
        },
        supersedesScopeId: transition?.supersedesScopeId
          ?? lastScopeId
          ?? undefined,
      })
      if (seq !== generateSeq.current) return
      applyConsideredRound(
        cs, seq, roundHypothesis, roundObjective, transition === null)
      if (transition) finishFeedbackTransition(transition, prior)
      setRecognition(null)
      setRecognitionTransition(null)
    } catch (err) {
      if (seq !== generateSeq.current) return
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  // Change the primary to another candidate: promote the chosen use-case and demote the old primary.
  // The server records both relationship changes as user overrides of the recognition proposal.
  function makePrimary(useCaseId: string) {
    setScopeSecondary(prev => {
      const withoutChosen = prev.filter(id => id !== useCaseId)
      return scopePrimary !== null && scopePrimary !== useCaseId
        ? [...withoutChosen, scopePrimary]
        : withoutChosen
    })
    setScopePrimary(useCaseId)
  }

  function removeSecondary(useCaseId: string) {
    setScopeSecondary(prev => prev.filter(id => id !== useCaseId))
  }

  async function generate(e: FormEvent) {
    e.preventDefault()
    const objective = goal.trim()
    // Both the hypothesis and the objective are required: the considered-set intake governs
    // against a stated hypothesis. Same early-return style as the objective check.
    if (!hypothesis.trim() || !objective) return
    // A register batch is confirming or in flight: a new round would replace the rows the
    // human is approving, letting their registrations complete out of view.
    if (feedbackLocked) return
    // Phase 1B (intent_confirmation_ui): Generate first RECOGNISES the objective and hands the
    // proposed scope to the human. Nothing is generated until they confirm/broaden. Flag off →
    // fall straight through to today's one-shot considered-set call below.
    if (confirmationUi) {
      void recognize(objective)
      return
    }
    const seq = ++generateSeq.current
    setNotice('')
    setScopeChanged(false)
    setGenerating(true)
    // Slice 3, the chosen policy arm: NOTHING is dropped here. The round on screen — and the
    // human's picks in it — survive until `applyConsideredRound` replaces them, so a failed
    // revision costs nothing. The other arm lives entirely in this block: with the constant
    // flipped, the round is cleared at submit and REVISE_SUBMIT_COPY has already said so.
    if (CLEAR_RESULTS_AT_REVISE_SUBMIT && briefSubmitted) {
      setGenerated(null)
      setScreenedTarget(null)
      clearSets()
      clearFeedback()
    }
    try {
      // The governed considered-set endpoint: same gauntlet-validated FeatureSet[] as
      // recommend-sets, returned as `alternatives`, plus a server-side intent_id that later
      // governs these candidates into a signed contract. No fallback to a plain endpoint on 503
      // (one provider, identical failure — show the honest notice).
      const cs = await contractConsideredSet(hypothesis.trim(), objective, {
        catalogSource: source.trim() || undefined,
        targetRef: target.trim() || undefined,
      })
      if (seq !== generateSeq.current) return
      applyConsideredRound(cs, seq, hypothesis.trim(), objective)
    } catch (err) {
      if (seq !== generateSeq.current) return
      // A FIRST generation has nothing to keep, so its failure clears the empty round it was
      // building and the screen falls to its error shell. A REVISION does have something to
      // keep: the policy stated in the drawer promises the visible run survives a failed
      // request, and throwing away a human's candidates and selections because a provider
      // timed out is exactly the invalidation this slice exists to prevent. The notice reports
      // the failure; the run stays, with its picks.
      if (!briefSubmitted || CLEAR_RESULTS_AT_REVISE_SUBMIT) {
        setGenerated(null)
        setScreenedTarget(null)
        clearSets()
        clearFeedback()
      }
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  // Whole-round feedback: release mode re-runs recognition and requires another human scope
  // confirmation before generation. Emergency legacy mode retains the direct considered-set call.
  async function sendSetFeedback(e: FormEvent) {
    e.preventDefault()
    if (setFbInFlight.current) return
    const instruction = setFbInstruction.trim()
    if (!instruction || setFbRounds >= FEEDBACK_ROUNDS) return
    if (feedbackLocked || generating || recognition !== null) return
    const seq = ++generateSeq.current
    setFbInFlight.current = true
    setNotice('')
    setSetFbBusy(true)
    try {
      if (confirmationUi) {
        if (!lastScopeId) {
          throw new Error("Feedback requires a prior confirmed scope")
        }
        const rec = await contractRecognitions(roundHypothesis, roundObjective, {
          feedback: instruction,
          supersedesScopeId: lastScopeId,
        })
        if (seq !== generateSeq.current) return
        setRecognition(rec)
        setRecognitionTransition({
          feedback: instruction,
          supersedesScopeId: lastScopeId,
        })
        setScopePrimary(
          rec.candidates.find(c => c.relationship === 'primary')?.use_case_id ?? null)
        setScopeSecondary(
          rec.candidates.filter(c => c.relationship === 'secondary')
            .map(c => c.use_case_id))
        setScopeExpansion('exact')
        setScopeContexts(rec.modelling_contexts)
        setScopeEntity(rec.target_entity)
        return
      }
      // Feedback routes through the governed considered-set endpoint so the round mints a FRESH
      // intent over the guided set: post-feedback candidates become governable (the stale-intent
      // guard is lifted). The ROUND's snapshotted hypothesis + objective run, never a since-edited
      // input; scope cannot have drifted (a scope edit voids the round).
      const cs = await contractConsideredSet(roundHypothesis, roundObjective, {
        catalogSource: source.trim() || undefined,
        targetRef: target.trim() || undefined,
        feedback: instruction,
      })
      if (seq !== generateSeq.current) return
      const round = {
        sets: cs.alternatives, recommendation: cs.recommendation, rejections: cs.rejections,
      }
      setIntentId(cs.intent_id)
      setGenerationRunId(cs.generation_run_id ?? null)
      // Pins read the selection AS THE RESPONSE LANDS (the mirrors), not as it stood at submit.
      const prev = generatedRef.current ?? []
      const pinned = prev.filter(c =>
        c.key in selectedRef.current || registeredRef.current[c.key] !== undefined)
      const keptSelected = pinned
        .filter(c => registeredRef.current[c.key] === undefined).length
      const replaced = prev.length - pinned.length
      const pinnedKeys = new Set(pinned.map(c => c.key))
      const fresh: GeneratedCandidate[] = []
      const lenses: string[] = []
      for (const [setIndex, set] of round.sets.entries()) {
        if (set.features.length === 0) continue
        lenses.push(set.lens)
        for (const [featureIndex, idea] of set.features.entries()) {
          fresh.push({
            kind: 'generated',
            key: `g${seq}:${idea.option_id ?? `${setIndex}:${featureIndex}:${idea.name}`}`,
            idea,
            lenses: [set.lens],
          })
        }
      }
      setGenerated([
        ...pinned.map((c): GeneratedCandidate => ({ ...c, kept: true, lenses: [] })),
        ...fresh,
      ])
      // The intent was refreshed above (setIntentId(cs.intent_id)) so the FRESH candidates are
      // governable; the kept pins came from a prior generation and are excluded by governableCount.
      // Kept rows left the sets model, so their selection origins go neutral: a kept pick
      // reads as kept in the tray, never as a pick from a set that no longer exists.
      setSelected(prev => Object.fromEntries(
        Object.entries(prev).map(([key, origin]) =>
          pinnedKeys.has(key) ? [key, null] : [key, origin])))
      setSetLenses(lenses)
      setRecommendation(round.recommendation)
      setActiveLens(
        lenses.length > 1
          ? round.recommendation !== null && lenses.includes(round.recommendation.recommended_lens)
            ? round.recommendation.recommended_lens
            : lenses[0]
          : null)
      setRejections(round.rejections)
      setNeedsSetup(cs.needs_setup ?? [])
      setRejectionsOpen(false)
      clearResultView()
      setConfirmingBatch(false)
      setConfirmingGovern(false)
      setSetFbRounds(r => r + 1)
      setSetFbRecords(records => [...records, {
        round: records.length + 1, user: getSession().user, instruction,
        kept: keptSelected, replaced,
      }])
      setSetFbInstruction('')
      // Kept candidates keep their consumed refine rounds; replaced ones drop theirs.
      setRefines(prevMap => Object.fromEntries(
        Object.entries(prevMap).filter(([key]) => pinnedKeys.has(key))))
      // A feedback round is a landed round too — it replaces what is on screen, so it announces.
      setRoundToken(token => token + 1)
    } catch (err) {
      if (seq !== generateSeq.current) return
      // The round never ran: candidates stay, no round is consumed.
      fail(err)
    } finally {
      // Unconditional: only one feedback flight can exist (reentry ref), so no newer feedback
      // owns the flag, and a scope edit or newer generate already reset it anyway.
      setFbInFlight.current = false
      setSetFbBusy(false)
    }
  }

  function patchRefine(key: string, patch: (prev: RefineState) => RefineState) {
    setRefines(prev => ({ ...prev, [key]: patch(prev[key] ?? EMPTY_REFINE) }))
  }

  function toggleRefine(key: string) {
    patchRefine(key, prev => ({ ...prev, open: !prev.open }))
  }

  // Per-candidate feedback: one engine run, re-checked by the gauntlet, recorded under the
  // session user. The result is a suggestion; the candidate changes only on Approve revision.
  async function sendRefine(candidate: GeneratedCandidate) {
    const { key, idea } = candidate
    if (refineInFlight.current.has(key)) return
    const state = refines[key] ?? EMPTY_REFINE
    const instruction = state.instruction.trim()
    if (!instruction || state.rounds >= FEEDBACK_ROUNDS) return
    if (feedbackLocked) return
    refineInFlight.current.add(key)
    setNotice('')
    patchRefine(key, prev => ({ ...prev, busy: true, rejection: null, error: null }))
    try {
      const result = await refineCandidate(
        {
          name: idea.name, description: idea.description, derives_from: idea.derives_from,
          aggregation: idea.aggregation, grain_table: idea.grain_table,
        },
        instruction,
        // Entity is always null: refine plans through the same one-catalog engine, and the
        // screen no longer collects a cross-catalog entity (see the E4 note at the top).
        source.trim() || null, null, target.trim() || null,
        // The current goal rides along so the engine revises against the objective, not the
        // instruction alone.
        goal.trim() || null,
      )
      // The row may have registered or been replaced while the engine ran: registered
      // candidates take no feedback, and a gone row has nothing to revise. Drop the result.
      const live = (generatedRef.current ?? []).some(c => c.key === key)
      if (!live || registeredRef.current[key] !== undefined) return
      if ('revised' in result) {
        const revised = result.revised
        patchRefine(key, prev => ({
          ...prev, rounds: prev.rounds + 1, pending: revised, pendingRound: prev.rounds + 1,
          pendingInstruction: instruction, rejection: null, error: null, instruction: '',
        }))
      } else {
        const rejected = result.rejected
        // The gauntlet rejected the revision: the round is still consumed, and the candidate
        // is unchanged. The typed instruction stays for the human to adjust.
        patchRefine(key, prev => ({
          ...prev, rounds: prev.rounds + 1, pending: null, rejection: rejected, error: null,
        }))
      }
    } catch (err) {
      const live = (generatedRef.current ?? []).some(c => c.key === key)
      if (live && registeredRef.current[key] === undefined) {
        if (err instanceof ApiError && err.status === 503) {
          // A 503 is a whole-deployment condition, not this row's: it goes to the one top notice
          // (carrying the server's own sentence) rather than being repeated on every row.
          fail(err)
        } else {
          patchRefine(key, prev => ({
            ...prev, error: err instanceof ApiError ? err.detail : String(err),
          }))
        }
      }
    } finally {
      refineInFlight.current.delete(key)
      setRefines(prev => key in prev
        ? { ...prev, [key]: { ...prev[key], busy: false } }
        : prev)
    }
  }

  // The human accepts the engine's revision: the candidate's data is replaced locally. The key
  // is stable, so selection state survives; registration still requires the tray's confirm.
  function approveRevision(key: string) {
    // Inert under feedbackLocked (the buttons also disable): an approved revision must never
    // diverge from the spec a confirming or in-flight register batch is writing.
    if (feedbackLocked) return
    const pending = (refines[key] ?? EMPTY_REFINE).pending
    if (pending === null) return
    setGenerated(prev => prev === null
      ? prev
      : prev.map(c => (c.key === key ? { ...c, idea: pending } : c)))
    patchRefine(key, prev => ({
      ...prev, pending: null, appliedRound: prev.pendingRound, open: false,
    }))
    // The revision block (holding focus) unmounts: move focus to the candidate row.
    focusTarget.current = `wb-row-${key}`
  }

  // The human declines: the revision is discarded, the candidate untouched. The consumed round
  // stays consumed; the engine ran.
  function revertRevision(key: string) {
    if (feedbackLocked) return
    patchRefine(key, prev => ({ ...prev, pending: null }))
    focusTarget.current = `wb-row-${key}`
  }

  async function draftCandidates(e: FormEvent) {
    e.preventDefault()
    if (draftInFlight.current) return
    // Capture the source once for the whole batch: every recipe drafts against one snapshot,
    // so a mid-batch scope edit cannot split the batch across sources.
    const snapshotSource = source.trim()
    const lines = describeText.split('\n').map(line => line.trim()).filter(Boolean)
    if (lines.length === 0 || !snapshotSource) return
    const seq = ++draftSeq.current
    draftInFlight.current = true
    setNotice('')
    setDraftErrors([])
    setDrafting(true)
    const fresh: DraftCandidate[] = []
    const failedLines: string[] = []
    const lineErrors: string[] = []
    let providerErr: ApiError | null = null
    try {
      // Sequential, in line order: deterministic and kind to the backend. A rejected line is
      // isolated; the surviving lines still draft.
      for (let i = 0; i < lines.length; i++) {
        const query = lines[i]
        try {
          const recipe = await featureRecipe(query, snapshotSource)
          fresh.push({
            kind: 'draft' as const, key: `d${seq}:${i}`, name: slugFrom(query),
            description: query, recipe, snapshotSource,
          })
        } catch (err) {
          failedLines.push(query)
          if (err instanceof ApiError && err.status === 503) {
            // A 503 is a whole-deployment condition, not a per-line problem: it surfaces once, in
            // the server's own words, through the same notice the generate path uses — never as N
            // identical line errors. The line itself is still kept for retry, below.
            providerErr = err
          } else {
            lineErrors.push(
              `Line ${i + 1}: ${err instanceof ApiError ? err.detail : String(err)}`)
          }
        }
      }
      if (seq !== draftSeq.current) return
      if (fresh.length > 0) setDrafts(prev => [...prev, ...fresh])
      // Keep only the failed lines so a retry is one click away; a clean batch clears fully.
      setDescribeText(failedLines.join('\n'))
      setDraftErrors(lineErrors)
      if (providerErr) fail(providerErr)
    } finally {
      draftInFlight.current = false
      if (seq === draftSeq.current) setDrafting(false)
    }
  }

  function deselect(key: string) {
    setSelected(prev => {
      if (!(key in prev)) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }

  function renameDraft(key: string, value: string) {
    setDrafts(prev => prev.map(d => (d.key === key ? { ...d, name: value } : d)))
    // A draft without a name cannot be registered: drop it from the selection too.
    if (!value.trim()) deselect(key)
  }

  function toggleSelect(key: string, origin: string | null) {
    // Changing the selection backs out of either confirm step: the confirm copy must always
    // describe exactly what will be registered or governed.
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    setSelected(prev => {
      const next = { ...prev }
      if (key in next) {
        delete next[key]
      } else {
        next[key] = origin
      }
      return next
    })
  }

  // Take this set: select every unregistered feature of the lens, stamping it as picked from
  // that set. Picks made from other sets keep their own origins (a la carte mixing).
  function takeSet(lens: string) {
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    setActiveLens(lens)
    let skipped = 0
    setSelected(prev => {
      const next = { ...prev }
      for (const c of generated ?? []) {
        if (!c.lenses.includes(lens) || registered[c.key]) continue
        // A4: batch selection honors the server's verdicts — a blocked candidate is skipped
        // and COUNTED, never silently included and never silently dropped.
        const entry = c.idea.option_id ? optionActions[c.idea.option_id] : undefined
        if (entry && !entry.allowed_actions.includes('create_contract')) {
          skipped += 1
          continue
        }
        next[c.key] = lens
      }
      return next
    })
    setNotice(skipped > 0
      ? `Skipped ${skipped} candidate${skipped === 1 ? '' : 's'} that need${skipped === 1 ? 's' : ''} `
        + 'a decision first — see the actionable section on each card.'
      : '')
  }

  async function confirmRegistration() {
    if (batchInFlight.current) return
    const batch = allCandidates.filter(c =>
      c.key in selected && !registered[c.key] && (c.kind === 'generated' || c.name.trim() !== ''))
    if (batch.length === 0) return
    batchInFlight.current = true
    setBatchBusy(true)
    setNotice('')
    try {
      // Sequential, one request per candidate. A failure marks its candidate and the batch
      // continues; the failed candidate stays selected for retry.
      for (const candidate of batch) {
        try {
          const id = await registerFeature(specFor(candidate))
          let freshness: FeatureFreshness | null = null
          try {
            freshness = await featureFreshness(id)
          } catch {
            // Freshness is advisory on this note: omit the chip rather than fail the
            // registration UI.
          }
          setRegistered(prev => ({ ...prev, [candidate.key]: { id, freshness } }))
          deselect(candidate.key)
          setErrors(prev => {
            if (!(candidate.key in prev)) return prev
            const next = { ...prev }
            delete next[candidate.key]
            return next
          })
        } catch (err) {
          setErrors(prev => ({
            ...prev,
            [candidate.key]: err instanceof ApiError ? err.detail : String(err),
          }))
        }
      }
    } finally {
      batchInFlight.current = false
      setBatchBusy(false)
      setConfirmingBatch(false)
    }
  }

  // Govern the selected GENERATED candidates into signed contracts, mirroring confirmRegistration:
  // sequential, one candidate at a time, per-candidate try/catch so a failure marks that candidate
  // and the batch continues. Only fresh (non-kept) generated candidates are in the current intent's
  // considered-set snapshot; chosenSource is legacy-only and chosenOptionId is the opaque option id.
  async function confirmGovern() {
    if (governInFlight.current) return
    const iid = intentId
    if (!iid) return
    const batch = allCandidates.filter(
      (c): c is GeneratedCandidate =>
        c.key in selected && !governed[c.key] && c.kind === 'generated' && !c.kept
        && (refines[c.key]?.appliedRound ?? null) === null)   // refined => diverges from the snapshot
    if (batch.length === 0) return
    governInFlight.current = true
    setGovernBusy(true)
    setNotice('')
    try {
      for (const candidate of batch) {
        try {
          const d = await contractDraft(
            iid,
            'alternative',
            candidate.idea.option_id ?? candidate.idea.name,
            '',
            generationRunId ?? undefined,
          )
          const c = await contractConfirm(d.draft, iid, d.choice_id)
          setGoverned(prev => ({ ...prev,
            [candidate.key]: { contractId: c.contract_id, version: c.version } }))
          deselect(candidate.key)
          setErrors(prev => {
            if (!(candidate.key in prev)) return prev
            const next = { ...prev }; delete next[candidate.key]; return next
          })
        } catch (err) {
          setErrors(prev => ({ ...prev,
            [candidate.key]: err instanceof ApiError ? err.detail : String(err) }))
        }
      }
    } finally {
      governInFlight.current = false
      setGovernBusy(false)
      setConfirmingGovern(false)
    }
  }

  // Tray mix note: where the picks came from. Kept rows carry no lens claim (their origin sets
  // were replaced), so they read as kept picks, never as picks from the currently-viewed set.
  // Honest cross-set copy: each feature was safety-checked at generation within its own set;
  // there is NO set-level re-check of the human's mix (tracked follow-up), so the note must not
  // claim one.
  const keptPicked = selectedCandidates
    .filter(c => c.kind === 'generated' && c.kept === true).length
  const lensNote = originLenses.length > 1
    ? `mixed from ${originLenses.length} sets · each feature was safety-checked at generation; `
      + 'your approval registers them individually'
    : originLenses.length === 1 && originLenses[0] !== undefined
      ? `from the ${lensLabel(originLenses[0])} set`
      : null
  // Which of the current picks the server will also sign into contracts, in words. Governability
  // is the SERVER's verdict (an open intent + a fresh, unrefined generated candidate); the rail
  // reports it and never re-derives it. Every arm names what CAN still happen, because "cannot be
  // governed" is a fact about this round's snapshot, not a judgement on the candidate.
  const governabilityNote = intentId === null
    ? 'No governing intent is open for this round, so these can be registered but not signed '
      + 'into contracts.'
    : governableCount === 0
      ? 'None of these can be governed from this round: a pick kept from an earlier round, a '
        + "revised candidate, or your own draft sits outside this round's governable snapshot. "
        + 'Registering still works.'
      : governableCount === selectedCount
        ? selectedCount === 1
          ? 'It can also be governed into a signed contract.'
          : `All ${selectedCount} can also be governed into signed contracts.`
        : `${governableCount} of ${selectedCount} can also be governed into signed contracts; `
          + 'the rest came from an earlier round, a revision, or your own drafts, and can only '
          + 'be registered.'

  const mixNote = multiSet && selectedCount > 0
    ? keptPicked > 0
      ? lensNote !== null
        ? `${lensNote} · ${keptPicked} kept from an earlier round`
        : 'kept from an earlier round'
      : lensNote
    : null

  return (
    // Carry the current round's governing intent on the DOM so a later task can govern these
    // candidates into a signed contract; undefined until the first successful generate.
    // `data-phase` publishes the one derived view phase the shell renders from.
    <section data-intent-id={intentId ?? undefined} data-phase={phase}>
      <div className="gates" role="list" aria-label="Where you are in the loop">
        {/* E4 cutover: cells 1 and 2 name what the ONE engine actually does. Cell 1 folds in the
            two human steps that sit between the brief and generation — the scope confirmation
            (always) and the one-click unit of analysis (optional, and skipping it never blocks).
            Cell 2 names the engine's real output: authored recipes and model intents planned over
            the frozen catalog context, never the deleted free-form column generator. */}
        <Gate
          state={gate1}
          who="You"
          title="State the goal"
          sub="Nothing generates until you confirm the scope, and optionally the unit of analysis."
        />
        <Gate
          state={gate2}
          who="Engine"
          title="Plan over the catalog"
          sub="Governed recipes and model intents over the catalog's confirmed meaning."
        />
        <Gate
          state={gate3}
          who="You"
          title="Compare, mix, give feedback"
          sub="Take a set or pick a la carte across sets."
        />
        <Gate
          state={gate4}
          who="You"
          title="You approve"
          sub="Nothing is saved or governed without your click, under your name."
        />
      </div>
      {/* The blocking notice is a platform fact about the deployment, not a property of the intake
          form it used to live inside: it renders above whichever shell is showing. */}
      {notice && (
        <div role="alert" className="callout callout--warn">
          <CalloutGlyph d={WARN_GLYPH} />
          <div className="callout-body">
            <p>{notice}</p>
          </div>
        </div>
      )}

      {/* ── The post-submit run deck ──────────────────────────────────────────────────────────
          Replaces the intake form once a round's snapshot exists. Three cards answer the four
          questions the old page made the human scroll for: what did I ask, what came back, and
          what do I do now. The brief is quoted from the ROUND SNAPSHOT and is not editable here —
          that is the whole point. Editing the live hypothesis input beside results generated from
          different text was the review's Critical finding, and the only way back to the fields is
          the explicit Revise brief control. */}
      {/* Slice 3: the deck stays up while the drawer is open. The run's identity — the brief that
          produced what is on screen — must remain readable beside a draft of its replacement, or
          the human is editing one thing while reading another, which is the Critical finding this
          workspace exists to close. */}
      {(!formOpen || reviseDrawerOpen) && (
        <div className="panel run-deck">
          <article className="deck-card">
            <p className="micro-label">Your submitted brief</p>
            <blockquote className="deck-quote">{roundHypothesis}</blockquote>
            <p className="deck-objective">
              Goal: {roundObjective.trim() !== '' ? roundObjective : 'not set'}
            </p>
            <div className="deck-chips">
              <span className="badge mono">
                catalog · {source.trim() !== '' ? source.trim() : 'not set'}
              </span>
              {/* `screenedTarget` is the target AS THE ROUND WAS SCREENED; before a considered set
                  lands (scope review) there is no such snapshot yet, so the live field is the only
                  truth there — and a target edit voids the round, so the two cannot drift. */}
              <span className="badge mono" title={(screenedTarget ?? target.trim()) || undefined}>
                target · {screenedTarget ?? (target.trim() !== '' ? target.trim() : 'not set')}
              </span>
            </div>
            <div className="deck-foot">
              <span className="micro-label">Submitted snapshot</span>
              {/* Slice 3: the ONE revise affordance. It opens the drawer; while the drawer is
                  open the trigger is gone, so the screen never offers two ways into the same
                  edit. Focus returns here when the drawer closes. */}
              {!reviseDrawerOpen && (
                <button
                  type="button"
                  className="btn"
                  id="wb-revise-open"
                  aria-haspopup="dialog"
                  onClick={() => setReviseOpen(true)}
                >
                  Revise brief
                </button>
              )}
            </div>
          </article>

          <article className="deck-card">
            <p className="micro-label">Engine output</p>
            {generated === null ? (
              <>
                {/* Honest absence: no round has produced candidates for this brief yet. Structure
                    stays, the number does not get invented. */}
                <span className="deck-metric deck-metric--absent">No candidates yet</span>
                <span className="deck-metric-label">
                  {phase === 'scope_review'
                    ? 'The run waits on your scope confirmation.'
                    : 'The engine has not returned a round for this brief.'}
                </span>
              </>
            ) : (
              <>
                <span className="deck-metric tabular-nums">
                  {generated.length} {generated.length === 1 ? 'candidate' : 'candidates'}
                </span>
                <span className="deck-metric-label tabular-nums">
                  {setLenses.length} {setLenses.length === 1 ? 'set' : 'sets'}
                  {selectedCount > 0 ? ` · ${selectedCount} selected` : ''}
                </span>
              </>
            )}
            <ul className="deck-detail">
              {screenedTarget !== null && <li>Screened against {screenedTarget}</li>}
              {rejections.length > 0 && (
                <li className="tabular-nums">
                  {rejections.length} {rejections.length === 1 ? 'rejection' : 'rejections'} listed
                  below
                </li>
              )}
              {/* Says a recommendation EXISTS; the advisory pick and its caveat stay together
                  where the sets are compared, never summarised into a verdict up here. */}
              {recommendation !== null && <li>Recommendation included</li>}
            </ul>
          </article>

          <article className="deck-card deck-card--action">
            <p className="micro-label">Your next action</p>
            {/* The focus target for a landed round, and the page's one stage headline. */}
            <h2 id="wb-stage-heading" tabIndex={-1} ref={stageHeadingRef} className="deck-stage">
              {stage.title}
            </h2>
            <p className="deck-next">{stage.next}</p>
          </article>
        </div>
      )}

      {/* Polite announcement of a landed round. Deliberately NOT role="status": the screen already
          has status regions whose identity tests and users rely on. */}
      <div aria-live="polite" className="visually-hidden">{liveMessage}</div>

      {/* ── Slice 3: the revise drawer ──────────────────────────────────────────────────────────
          The drawer is a SHELL around the brief form the draft state already renders — the same
          fields, the same handlers, the same submit. That is deliberate: two copies of an intake
          form is how the field a human edits stops being the field the engine reads. What the
          drawer adds is the frame the review asks for — the snapshot it was populated from, the
          policy stated BEFORE submission, and two explicit outcomes.

          It is deliberately NOT `aria-modal`, and there is no backdrop. A human revises a brief
          BECAUSE of what the results say, so making them dismiss the drawer to read the rows
          they are reacting to would be the same "output out of reach" defect in a new place. The
          run behind stays visible, readable and interactive — and the second path out of this
          form ("Write definitions myself") opens its panel into a page nothing is covering. */}
      {formOpen && (
        <div
          className={reviseDrawerOpen ? 'drawer' : 'panel'}
          id="wb-brief-form"
          role={reviseDrawerOpen ? 'dialog' : undefined}
          aria-labelledby={reviseDrawerOpen ? 'wb-revise-title' : undefined}
          onKeyDown={reviseDrawerOpen
            ? event => { if (event.key === 'Escape') closeRevise() }
            : undefined}
          onFocusCapture={e => {
            const el = e.target as HTMLElement
            briefTextFocused.current = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
          }}
          onBlurCapture={() => { briefTextFocused.current = false }}
        >
          {reviseDrawerOpen && (
            <header className="drawer-head">
              <div>
                <p className="micro-label">New draft from your submitted brief</p>
                <h2 id="wb-revise-title" style={{ margin: 0 }}>Revise the brief</h2>
              </div>
              <button
                type="button"
                className="btn"
                aria-label="Close revise brief"
                onClick={closeRevise}
              >
                ×
              </button>
            </header>
          )}
          {reviseDrawerOpen && (
            <div className="drawer-note">
              {/* Populated FROM the snapshot: after a round lands the fields already hold exactly
                  what was submitted. They diverge only when the human edits them — which must
                  never be undone silently, so divergence is named and reversible instead. */}
              {briefDiffersFromSnapshot ? (
                <>
                  <p>
                    These fields hold your edits, not the brief this run was generated from. The
                    submitted brief is quoted above and still describes the results below.
                  </p>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => {
                      setHypothesis(roundHypothesis)
                      setGoal(roundObjective)
                    }}
                  >
                    Restore the submitted brief
                  </button>
                </>
              ) : (
                <p>Populated from the brief this run was generated from.</p>
              )}
            </div>
          )}
          {/* Slice 1's caret-protection banner. It belongs to the INLINE surface only: the drawer
              says the same thing in its own note and its own actions, and showing both would be
              the two-affordance problem in one panel. */}
          {briefSubmitted && !reviseDrawerOpen && (
            <div className="brief-revising">
              <p className="hint" style={{ margin: 0 }}>
                Revising the brief. The results below were generated for the submitted brief and are
                unchanged until a new round lands.
              </p>
              {/* The review's rule: revising has two explicit outcomes and neither is silent. This
                  is the Cancel arm — it returns to the run exactly as it was. */}
              <button
                type="button"
                className="btn"
                onClick={() => setBriefRevising(false)}
              >
                Keep submitted brief
              </button>
            </div>
          )}
          <form onSubmit={generate} style={{ display: 'grid', gap: 16, margin: 0 }}>
            <div className="field" style={{ maxWidth: 640 }}>
              <label htmlFor="wb-hypothesis">Hypothesis</label>
              <input
                id="wb-hypothesis"
                value={hypothesis}
                onChange={e => setHypothesis(e.target.value)}
                placeholder="e.g. customers whose balance is draining are about to leave"
                style={{ height: 40 }}
              />
            </div>
            <div className="field" style={{ maxWidth: 640 }}>
              <label htmlFor="wb-goal">Prediction goal</label>
              <input
                id="wb-goal"
                value={goal}
                onChange={e => setGoal(e.target.value)}
                placeholder="e.g. predict customer churn in the next 90 days"
                style={{ height: 40 }}
              />
              <div
                className="hint"
                style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}
              >
                <span>
                  Both paths use it: the engine generates against it, and written definitions attach
                  to it.
                </span>
                <span>Try</span>
                <button type="button" className="role-chip" onClick={() => setGoal(EXAMPLE_GOAL)}>
                  {EXAMPLE_GOAL}
                </button>
              </div>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 20 }}>
              <div className="field" style={{ flex: '1 1 220px' }}>
                <label htmlFor="wb-source">Catalog source</label>
                {/* Scope edits clear candidates, so both lock under feedbackLocked: a row
                    must not leave view while its registration is being written. */}
                <input
                  id="wb-source"
                  value={source}
                  onChange={e => changeSource(e.target.value)}
                  placeholder="e.g. deposits"
                  disabled={feedbackLocked}
                />
                <p className="hint" style={HELP_STYLE}>
                  Required. The catalog the engine plans over: generation reads one catalog's
                  governed meaning. Cross-catalog generation returns in a later release.
                </p>
              </div>
              <div className="field" style={{ flex: '1 1 220px' }}>
                <label htmlFor="wb-target">Target column</label>
                <input
                  id="wb-target"
                  value={target}
                  onChange={e => changeTarget(e.target.value)}
                  placeholder="e.g. public.labels.churned"
                  disabled={feedbackLocked}
                />
                <p className="hint" style={HELP_STYLE}>
                  What you are predicting. Candidates are screened against it server-side, so leaky
                  features never reach you.
                </p>
              </div>
            </div>
            <div className="paths">
              <button
                type="submit"
                className="path path-generate"
                disabled={!hypothesis.trim() || !goal.trim() || generating || feedbackLocked}
              >
                <span className="k">Path 1 · The engine</span>
                <span className="t">
                  <PathGlyph>
                    <circle cx="8" cy="8" r="6.2" />
                    <path d="M8 5v6M5 8h6" />
                  </PathGlyph>
                  {/* Slice 3: the control names what it will actually do. Submitting from the
                      drawer starts a REVISED round over a run that already exists; calling that
                      "Generate candidate sets" hides the replacement from the click that causes
                      it. */}
                  {generating
                    ? 'Generating'
                    : reviseDrawerOpen ? 'Generate revised round' : 'Generate candidate sets'}
                </span>
                <span className="d">
                  Governed recipes and model intents planned over this catalog's confirmed meaning,
                  grouped by operation class — blockers and their next steps named, never hidden.
                </span>
              </button>
              <button
                type="button"
                className="path path-describe"
                aria-pressed={describeOpen}
                aria-controls="wb-describe-panel"
                onClick={() => setDescribeOpen(open => !open)}
              >
                <span className="k">Path 2 · Your definitions</span>
                <span className="t">
                  <PathGlyph>
                    <path d="M3 13h10M4 10.5 10.8 3.7a1.4 1.4 0 0 1 2 2L6 12.5l-2.8.8z" />
                  </PathGlyph>
                  Write definitions myself
                </span>
                <span className="d">
                  One definition per line; each becomes a draft candidate with its real join path,
                  drafted together.
                </span>
              </button>
            </div>
            {/* ── The two explicit outcomes, and the policy stated BEFORE either ────────────────
                Both sentences come from CLEAR_RESULTS_AT_REVISE_SUBMIT, so the promise the human
                reads and the behaviour they get are the same constant. Neither outcome is
                silent: Cancel writes nothing, Generate says what it replaces. */}
            {reviseDrawerOpen && (
              <div className="drawer-actions">
                <p className="hint" data-role="revise-policy">
                  {REVISE_SUBMIT_COPY} {REVISE_REPLACE_COPY}
                </p>
                <div className="decision-actions">
                  <button type="button" className="btn" onClick={closeRevise}>
                    Keep submitted brief
                  </button>
                </div>
              </div>
            )}
          </form>
        </div>
      )}

      {/* Phase 1B Gate #1: the human confirms/overrides/broadens the recognised scope BEFORE the
          considered set is generated. Rendered from the derived phase, which is exactly "a
          recognition is awaiting confirmation" and outranks an in-flight grounding call — the
          panel must not vanish under the click that started it. */}
      {phase === 'scope_review' && recognition !== null && (
        <div className="panel" id="wb-scope-panel">
          <h2>Confirm the scope</h2>
          <p className="hint" style={{ marginTop: 4 }}>
            We recognised what you're building. Confirm it, adjust it, or show every buildable
            recipe. Nothing generates until you confirm.
          </p>
          {recognition.status === 'ambiguous' && primaryCandidate !== null && (
            <p className="hint" role="status">
              The objective read as ambiguous — check the primary before confirming.
            </p>
          )}
          {/* What you are looking at, and what it cost. Five outcomes, five sentences — see
              scopeNotice/qualityNotice. */}
          {scopeNotice(recognition, primaryCandidate !== null) !== null && (
            <p role="status">{scopeNotice(recognition, primaryCandidate !== null)}</p>
          )}
          {qualityNotice(recognition) !== null && (
            <p className="hint" role="status" data-role="recognition-quality">
              {qualityNotice(recognition)}
            </p>
          )}
          {/* The proposals render whenever the recognizer made any — NOT only when one of them is
              the primary. A partial recovery whose primary was the discarded candidate, and an
              ambiguous answer that designated none, both have real alternatives to choose from;
              hiding them behind "no use-case was recognised" reported presence as absence. */}
          {recognition.candidates.length > 0 && (
            <>
              {primaryCandidate !== null && (
                <div className="scope-primary" data-role="primary">
                  <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                    <span className="badge recommended">Primary</span>
                    <span style={{ fontWeight: 600 }}>{primaryCandidate.display_name}</span>
                    <span className="badge">{primaryCandidate.confidence} confidence</span>
                  </div>
                  {primaryCandidate.evidence_spans.length > 0 && (
                    <ul className="scope-evidence" style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                      {primaryCandidate.evidence_spans.map(span => (
                        <li key={span} style={{ color: 'var(--ink-soft)' }}>“{span}”</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {secondaryCandidates.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <h3 style={{ margin: '0 0 8px' }}>
                    {primaryCandidate !== null ? 'Also in scope' : 'Alternatives'}
                  </h3>
                  <ul className="rows">
                    {secondaryCandidates.map(cand => (
                      <li
                        key={cand.use_case_id}
                        className="row"
                        style={{ alignItems: 'flex-start', gap: 10 }}
                      >
                        <div style={{ display: 'grid', gap: 6, flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                            <span className="badge">Secondary</span>
                            <span style={{ fontWeight: 600 }}>{cand.display_name}</span>
                            <span className="badge">{cand.confidence} confidence</span>
                          </div>
                          {cand.evidence_spans.length > 0 && (
                            <ul className="scope-evidence" style={{ margin: 0, paddingLeft: 18 }}>
                              {cand.evidence_spans.map(span => (
                                <li key={span} style={{ color: 'var(--ink-soft)' }}>“{span}”</li>
                              ))}
                            </ul>
                          )}
                        </div>
                        <button
                          type="button"
                          className="btn"
                          onClick={() => makePrimary(cand.use_case_id)}
                        >
                          Make primary
                        </button>
                        <button
                          type="button"
                          className="btn"
                          aria-label={`Remove ${cand.display_name}`}
                          onClick={() => removeSecondary(cand.use_case_id)}
                        >
                          Remove
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <label
                htmlFor="wb-scope-descendants"
                style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}
              >
                <input
                  id="wb-scope-descendants"
                  type="checkbox"
                  checked={scopeExpansion === 'include_descendants'}
                  onChange={e =>
                    setScopeExpansion(e.target.checked ? 'include_descendants' : 'exact')}
                  style={{ width: 18, height: 18 }}
                />
                Include all sub-use-cases?
              </label>
            </>
          )}
          {/* The target confirm block (intake build): the model's DRAFT reading of the prediction
              target, awaiting the human's signature. The extracted target drives the leakage veto,
              so it must not take effect as an unreviewed model pick. Absent intake (older backend,
              no LLM) renders nothing: the manual target field above carries the flow exactly as
              before.

              ▲ T7 (c), NB-2: the shows-doesn't-gate PIN path is now OUTCOME-FAMILY ONLY. The
              server stopped recording a literally-typed non-outcome column durably — typing a name
              in prose used to write it onto the very row the leakage gate reads, undisclosed,
              while CLICKING confirm on that same column was a refusal. So "you named it, already
              recorded" may only be said where the record actually exists; every other pin falls
              through to the confirm gate below, which asks for the acknowledgment out loud. */}
          {intake !== null && (
            <div className="scope-target" data-role="intake-target" style={{ marginTop: 16 }}>
              <h3 style={{ margin: '0 0 8px' }}>Prediction target</h3>
              {intakeReading !== null ? (
                intakeReading.target_provenance === 'exploring' ? (
                  <p role="status" style={{ margin: 0 }}>
                    <span className="badge">Exploring</span>{' '}
                    No target declared. Generation runs, but leakage checks are off — declare a
                    target any time in the target field above.
                  </p>
                ) : (
                  <div style={{ display: 'grid', gap: 8 }}>
                    <p role="status" style={{ margin: 0 }}>
                      <span className="badge recommended">Signed</span>{' '}
                      Target: <code>{intakeReading.target_ref}</code> — recorded as your decision.
                      Candidates are screened against it server-side.
                    </p>
                    {/* The SERVER's derivation from the concept registry, echoed back — never the
                        flag the client sent. A person cannot relabel a column by acknowledging
                        one, and the signed block must not read as if they had. */}
                    <p className="hint" style={{ margin: 0 }} data-role="signed-class">
                      Concept:{' '}
                      {intakeReading.target_concept
                        ? <code>{intakeReading.target_concept}</code>
                        : <>none registered</>}
                      {' · registry class: '}
                      <span className="mono">
                        {intakeReading.target_leakage_class ?? 'unregistered'}
                      </span>
                      {intakeReading.target_is_proxy && (
                        <> <span className="badge stale">proxy for the outcome</span></>
                      )}
                    </p>
                    {intakeAcknowledged !== '' && (
                      <p className="hint" style={{ margin: 0 }} data-role="acknowledged">
                        You acknowledged: {intakeAcknowledged}
                      </p>
                    )}
                  </div>
                )
              ) : intake.ticket.pinned && intake.ticket.target_column
                  && intake.ticket.target_leakage_class === 'outcome' ? (
                <p role="status" style={{ margin: 0 }}>
                  Target: <code>{intake.ticket.target_column}</code> ✓ (you named it) — edit the
                  target field above to change it.
                </p>
              ) : intake.ticket.target_column !== null ? (
                <div style={{ display: 'grid', gap: 8 }}>
                  <p style={{ margin: 0 }}>
                    I understood your target as: <code>{intake.ticket.target_column}</code>
                    {intake.target_detail?.ai_summary
                      ? <> — <em>{intake.target_detail.ai_summary}</em></>
                      : null}
                  </p>
                  {/* T7 (a): the proposal ABSTAINS unless the column's concept is outcome-family,
                      so a reading is not the same thing as a recommendation. Saying which one this
                      is costs one line and is the difference between an offer and a suggestion. */}
                  {intake.ticket.confidence === 'abstain' && (
                    <p className="hint" style={{ margin: 0 }} data-role="target-abstained">
                      A reading, not a recommendation: the platform did not commit to this target.
                    </p>
                  )}
                  <TargetTicketFacts intake={intake} />
                  {intake.ticket.contradiction !== null && (
                    <p className="hint" role="alert" style={{ margin: 0 }}>
                      Heads up: {intake.ticket.contradiction}.
                    </p>
                  )}
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                    <button
                      type="button" className="btn" disabled={intakeBusy}
                      onClick={() =>
                        answerIntake('confirmed', intake.ticket.target_column ?? undefined)}
                    >
                      Yes, that's my target
                    </button>
                    <button
                      type="button" className="btn" disabled={intakeBusy}
                      onClick={() => setIntakeCorrecting(v => !v)}
                    >
                      Change it
                    </button>
                    <button
                      type="button" className="btn" disabled={intakeBusy}
                      onClick={() => answerIntake('exploring')}
                    >
                      No target — just exploring
                    </button>
                  </div>
                  {intakeCorrecting && (
                    <div style={{ display: 'grid', gap: 8 }}>
                      {/* The model's ranked runners-up: correcting is one click, never a restart.
                          The free-text field below stays for a target the model never surfaced. */}
                      {(intake.runner_up_details ?? []).map(alt => (
                        <button
                          key={alt.ref} type="button" className="btn" disabled={intakeBusy}
                          style={{ justifySelf: 'start', textAlign: 'left' }}
                          onClick={() => answerIntake('corrected', alt.ref)}
                        >
                          <code>{alt.ref}</code>
                          {alt.ai_summary ? <> — {alt.ai_summary}</> : null}
                        </button>
                      ))}
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                      <label htmlFor="wb-intake-correction" style={{ alignSelf: 'center' }}>
                        Correct target
                      </label>
                      <input
                        id="wb-intake-correction"
                        value={intakeCorrection}
                        onChange={e => setIntakeCorrection(e.target.value)}
                        placeholder="e.g. public.labels.churned"
                        style={{ flex: '1 1 260px' }}
                      />
                      <button
                        type="button" className="btn"
                        disabled={intakeBusy || !intakeCorrection.trim()}
                        onClick={() => answerIntake('corrected', intakeCorrection.trim())}
                      >
                        Sign this target
                      </button>
                    </div>
                    </div>
                  )}
                </div>
              ) : (
                <div style={{ display: 'grid', gap: 8 }}>
                  <p style={{ margin: 0 }}>
                    No target detected in your objective. Type one into the target field above, or
                    explore without one.
                  </p>
                  {/* An abstention is an ANSWER, not a blank: the catalog's own outcome labels and
                      nearest proxies are what makes it one. Same block as the confirm branch. */}
                  <TargetTicketFacts intake={intake} />
                  <div>
                    <button
                      type="button" className="btn" disabled={intakeBusy}
                      onClick={() => answerIntake('exploring')}
                    >
                      No target — just exploring
                    </button>
                  </div>
                </div>
              )}
              {/* T7 (c) — THE ACKNOWLEDGMENT. The server's per-tier sentence, verbatim, with the
                  control that re-sends the SAME decision and the SAME ref carrying the person's
                  acknowledgment. Nothing here is composed: the three tiers say three different
                  things, and the difference between them is the whole point of the gate. */}
              {intakeAck !== null && (
                <div
                  className="hint" role="alert" data-role="intake-not-outcome"
                  style={{ display: 'grid', gap: 8, margin: '8px 0 0' }}
                >
                  <p style={{ margin: 0 }}>{intakeAck.detail}</p>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                    <button
                      type="button" className="btn" disabled={intakeBusy}
                      onClick={() => answerIntake(intakeAck.decision, intakeAck.ref, true)}
                    >
                      I understand — record this target anyway
                    </button>
                    <button
                      type="button" className="btn" disabled={intakeBusy}
                      onClick={() => { setIntakeAck(null); setIntakeCorrecting(true) }}
                    >
                      Pick a different target
                    </button>
                  </div>
                </div>
              )}
              {intakeError && (
                <p className="hint" role="alert" style={{ margin: '8px 0 0' }}>{intakeError}</p>
              )}
            </div>
          )}
          {/* Phase-2B SOFT dimensions (modelling context + prediction grain). Rendered whenever a
              recognition has landed — dimensions can be proposed WITHOUT a use-case — so it lives
              OUTSIDE the primary/no-primary branch. These are ranking nudges ONLY: editing them
              never narrows the scope and never rejects a recipe. */}
          <div className="scope-dimensions" style={{ marginTop: 16 }}>
            <h3 style={{ margin: '0 0 8px' }}>Modelling context &amp; entity (optional)</h3>
            <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
              {scopeContexts.map(ctx => (
                <span
                  key={ctx}
                  className="badge"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
                >
                  {ctx}
                  <button
                    type="button"
                    className="btn"
                    aria-label={`Remove context ${ctx}`}
                    onClick={() => setScopeContexts(prev => prev.filter(c => c !== ctx))}
                  >
                    Remove
                  </button>
                </span>
              ))}
              <select
                aria-label="Add modelling context"
                value=""
                onChange={e => {
                  const ctx = e.target.value
                  if (ctx) setScopeContexts(prev => (prev.includes(ctx) ? prev : [...prev, ctx]))
                }}
              >
                <option value="">Add context…</option>
                {MODELLING_CONTEXT_OPTIONS.filter(o => !scopeContexts.includes(o)).map(o => (
                  <option key={o} value={o}>{o}</option>
                ))}
              </select>
            </div>
            <div className="field" style={{ marginTop: 12, maxWidth: 320 }}>
              <label htmlFor="wb-scope-entity">Target entity</label>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <input
                  id="wb-scope-entity"
                  type="text"
                  value={scopeEntity ?? ''}
                  onChange={e => {
                    const v = e.target.value.trim()
                    setScopeEntity(v === '' ? null : v)
                  }}
                />
                <button type="button" className="btn" onClick={() => setScopeEntity(null)}>
                  Clear entity
                </button>
              </div>
            </div>
            {recognition.warnings.length > 0 && (
              <p className="hint" role="status" style={{ marginTop: 8 }}>
                We couldn't map part of what you described to a known context or entity.
              </p>
            )}
          </div>
          {/* B10 — the unit-of-analysis confirmation: yes/no on the DERIVED proposal, or one of
              the catalog's realistic spine-backed entities (a closed list, never free text).
              Optional: skipping it never gates the confirm button. */}
          {uoaProposal && (uoaProposal.proposed || uoaProposal.alternatives.length > 0) && (
            <div className="uoa-confirm" style={{ marginTop: 16 }}>
              <h3 style={{ margin: '0 0 8px' }}>Unit of analysis (optional)</h3>
              {uoaChoice ? (
                <p style={{ margin: 0 }}>
                  Predicting per <strong>{uoaChoice.entity}</strong>{' '}
                  (spine: {uoaChoice.spine_table} via {uoaChoice.spine_ref})
                  <button
                    type="button"
                    className="btn"
                    style={{ marginLeft: 8 }}
                    onClick={() => { setUoaChoice(null); setUoaPicking(false) }}
                  >
                    Change
                  </button>
                </p>
              ) : uoaPicking || !uoaProposal.proposed ? (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                  {uoaProposal.alternatives.map(a => (
                    <button
                      key={a.spine_ref}
                      type="button"
                      className="btn"
                      onClick={() => { setUoaChoice(a); setUoaPicking(false) }}
                    >
                      {a.entity} — {a.spine_table} via {a.spine_ref}
                    </button>
                  ))}
                </div>
              ) : (
                <div>
                  <p style={{ margin: '0 0 8px' }}>
                    You're predicting per <strong>{uoaProposal.proposed.entity}</strong>{' '}
                    (spine: {uoaProposal.proposed.spine_table} via{' '}
                    {uoaProposal.proposed.spine_ref}) — correct?
                  </p>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => setUoaChoice(uoaProposal.proposed as UoaOption)}
                    >
                      Yes
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => setUoaPicking(true)}
                    >
                      No — pick another
                    </button>
                  </div>
                </div>
              )}
              {uoaProposal.contradiction && (
                <p className="hint" role="status" style={{ marginTop: 8 }}>
                  {uoaProposal.contradiction}
                </p>
              )}
            </div>
          )}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 16 }}>
            {primaryCandidate !== null && (
              <button
                type="button"
                className="btn btn--primary"
                disabled={generating}
                onClick={() => void confirmScope()}
              >
                Confirm scope and generate
              </button>
            )}
            <button
              type="button"
              className="btn"
              disabled={generating}
              onClick={() => void broadenScope()}
            >
              Show all buildable recipes
            </button>
          </div>
        </div>
      )}

      {scopeChanged && (
        <p role="status" className="hint">
          Scope changed. Regenerate to refresh candidates.
        </p>
      )}

      {describeOpen && (
        <div className="panel" id="wb-describe-panel">
          <h2>Describe features</h2>
          <form onSubmit={draftCandidates} style={{ display: 'grid', gap: 12, margin: 0 }}>
            <div className="field" style={{ maxWidth: 640 }}>
              <label htmlFor="wb-describe">Describe the feature you want</label>
              <textarea
                id="wb-describe"
                rows={4}
                value={describeText}
                onChange={e => setDescribeText(e.target.value)}
                placeholder={DESCRIBE_PLACEHOLDER}
              />
              <p className="hint" style={HELP_STYLE}>
                Write one definition per line. Each line becomes a draft candidate you can name,
                adjust, and register together.
              </p>
            </div>
            {draftErrors.length > 0 && (
              <div style={{ display: 'grid', gap: 4 }}>
                {draftErrors.map(message => (
                  <p key={message} className="error" role="alert">
                    {message}
                  </p>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
              <button
                type="submit"
                className="btn"
                disabled={drafting || draftLines.length === 0 || !source.trim()}
              >
                {drafting
                  ? 'Drafting…'
                  : draftLines.length > 1
                    ? `Draft ${draftLines.length} candidates`
                    : 'Draft candidate'}
              </button>
              {!source.trim() && (
                <p className="hint">
                  Recipes read one catalog source. Set Catalog source above to draft.
                </p>
              )}
            </div>
          </form>
        </div>
      )}

      {generated?.length === 0 && (
        <>
          {/* T2: "no cards" and "nothing to say" are different rounds. When the setup lane holds
              entries it IS the answer, and it states the absence more precisely than this does —
              so this note stands down entirely rather than adding a sentence that blames the goal
              ("for that goal") and a remedy that would send a person to rewrite a question which
              was never the problem. */}
          {needsSetup.length === 0 && (
            <div className="empty" role="status">
              <p>No grounded candidates for that goal.</p>
              <p className="next">Rephrase the goal, or change the catalog source and generate again.</p>
            </div>
          )}
          {needsSetup.length > 0 && (
            <NeedsSetupPanel
              entries={needsSetup}
              open={needsSetupOpen}
              onToggle={() => setNeedsSetupOpen(open => !open)}
            />
          )}
          {/* An all-rejected round still shows WHY: rejections are never hidden. (When drafts
              exist the candidates block below renders the panel instead.) */}
          {rejections.length > 0 && allCandidates.length === 0 && (
            <RejectionsPanel
              rejections={rejections}
              open={rejectionsOpen}
              onToggle={() => setRejectionsOpen(open => !open)}
            />
          )}
        </>
      )}

      {/* ── Slice 2: the decision workspace ───────────────────────────────────────────────────
          Two columns in ONE document flow: the work, and the rail that says what the work will
          cost. The rail is a grid CELL, not an overlay — it sticks within its own column, so it
          can never cover a candidate's text (the review's accessibility constraint). Below
          768px the grid collapses to one column, the rail returns to the document immediately
          after the list, and only its tray pins to the bottom edge — with page padding under
          the list so the last row is never hidden behind it. */}
      {allCandidates.length > 0 && (
        // `data-tray` is mirrored here so the narrow-screen stylesheet can reserve room for the
        // pinned tray with a plain attribute selector — no :has(), and the reservation exists
        // only while there is something to pin.
        <div className="work-layout" data-tray={selectedCount > 0 || undefined}>
          <div className="work-main">
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginTop: 32 }}>
              <h2>{multiSet ? 'Proposed feature sets' : 'Proposed features'}</h2>
              <span className="micro-label tabular-nums">
                {multiSet && generated !== null ? (
                  <>
                    <span style={{ color: 'var(--accent)' }}>{setLenses.length}</span> sets ·{' '}
                    <span style={{ color: 'var(--accent)' }}>{generated.length}</span>{' '}
                    {generated.length === 1 ? 'feature' : 'features'}
                  </>
                ) : (
                  <>
                    <span style={{ color: 'var(--accent)' }}>{listCandidates.length}</span>{' '}
                    {listCandidates.length === 1 ? 'candidate' : 'candidates'}
                  </>
                )}
              </span>
            </div>
            <p className="hint" style={{ marginTop: 4 }}>
              <strong style={{ color: 'var(--ink)' }}>
                Nothing below enters the catalog without your approval.
              </strong>
              {hasDesignChecked &&
                ' Design-checked: structurally safe against leakage, staleness, and double-counting. Predictive value is proven later by backtests.'}
            </p>
            {screenedTarget && (
              <p className="hint" style={{ marginTop: 4 }}>
                Screened against <span className="mono">{screenedTarget}</span>: leaky candidates
                were rejected before reaching you.
              </p>
            )}
            {rejections.length > 0 && (
              <RejectionsPanel
                rejections={rejections}
                open={rejectionsOpen}
                onToggle={() => setRejectionsOpen(open => !open)}
              />
            )}
            {/* A round can serve cards AND hold setup work: the lane is not an empty-state, it is
                the fourth outcome, and it belongs beside the other three whenever it has entries. */}
            {needsSetup.length > 0 && (
              <NeedsSetupPanel
                entries={needsSetup}
                open={needsSetupOpen}
                onToggle={() => setNeedsSetupOpen(open => !open)}
              />
            )}
            {/* ── What this run's options are made of ─────────────────────────────────────────────
                A count is not a queue. Every number here is tallied from the `binding_state` the
                server put on each option of THIS run — nothing is authored, nothing is carried over
                from another run, and the strip simply does not render when the response carried no
                option-actions entries to count. Each state says what it MEANS, because "ambiguous"
                on its own reads as a failure when it is confirmation work nobody has done yet. */}
            {composition.length > 0 && (
              <div className="panel composition" id="wb-composition">
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
                  <h3 style={{ margin: 0 }}>What this run returned</h3>
                  <span className="micro-label tabular-nums">
                    <span style={{ color: 'var(--accent)' }}>{optionEntries.length}</span>{' '}
                    {optionEntries.length === 1 ? 'option' : 'options'}
                  </span>
                </div>
                <p className="hint" style={{ marginTop: 4 }}>
                  Counted from the binding state the engine returned for each option
                  {allCandidates.length !== optionEntries.length
                    && ` (${allCandidates.length} ${allCandidates.length === 1
                      ? 'candidate is' : 'candidates are'} on the list below)`}
                  .
                </p>
                <div
                  className="comp-bar"
                  role="img"
                  aria-label={composition
                    .map(row => `${row.count} ${row.label.toLowerCase()}`).join(', ')}
                >
                  {composition.map(row => (
                    <span
                      key={row.state}
                      className="comp-seg"
                      data-state={row.state}
                      style={{ width: `${(row.count / optionEntries.length) * 100}%` }}
                    />
                  ))}
                </div>
                <ul className="comp-key">
                  {composition.map(row => (
                    <li key={row.state}>
                      <span className="comp-dot" data-state={row.state} aria-hidden="true" />
                      <span className="comp-n tabular-nums">{row.count}</span>
                      <span>
                        <strong>{row.label}</strong>
                        {row.meaning !== null && <> — {row.meaning}</>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {multiSet && generated !== null && (
              <>
                <div className="sets">
                  {setLenses.map(lens => {
                    const feats = generated.filter(c => c.lenses.includes(lens))
                    const inTray = feats.filter(
                      c => c.key in selected || registered[c.key] !== undefined).length
                    const isActive = lens === activeLens
                    const thesis = LENS_THESES[lens]
                    return (
                      <div key={lens} className="set-card" data-active={isActive || undefined}>
                        <button
                          type="button"
                          className="set-card-view"
                          aria-pressed={isActive}
                          onClick={() => setActiveLens(lens)}
                        >
                          <span className="set-lens">
                            Lens · {lensLabel(lens)}
                            {recommendation?.recommended_lens === lens && (
                              <span className="badge recommended">Recommended</span>
                            )}
                          </span>
                          <span className="set-name">{lensLabel(lens)} set</span>
                          {thesis !== undefined && <span className="set-thesis">{thesis}</span>}
                          <span className="set-meta tabular-nums">
                            {feats.length} {feats.length === 1 ? 'feature' : 'features'} · all
                            design-checked
                            {inTray > 0 ? ` · ${inTray} in your tray` : ''}
                          </span>
                        </button>
                        <button
                          type="button"
                          className="btn set-take"
                          aria-label={`Take this set (${lensLabel(lens)})`}
                          onClick={() => takeSet(lens)}
                        >
                          Take this set
                        </button>
                      </div>
                    )
                  })}
                </div>
                {recommendation !== null && (
                  <div className="advice">
                    <p>
                      <strong>
                        Engine's pick: {lensLabel(recommendation.recommended_lens)}.
                      </strong>{' '}
                      {recommendation.reasoning}
                    </p>
                    <p className="advice-caveat">Caveat: {recommendation.caveat}</p>
                  </div>
                )}
              </>
            )}
            {/* ── Whole-round feedback, where the comparison happens ────────────────────────────
                Slice 2 moves this ABOVE the candidate list. It used to sit after every row, so on
                the reviewed run the one control that changes the whole round was hundreds of rows
                below the recommendation it disagrees with. Only an engine round can regenerate
                under guidance, so a drafts-only list still offers no panel. */}
            {hasGenerated && (
              <form className="setfb" onSubmit={sendSetFeedback}>
                <div className="field">
                  <label htmlFor="wb-setfb">Feedback on the whole round</label>
                  <div className="setfb-row">
                    <input
                      id="wb-setfb"
                      value={setFbInstruction}
                      onChange={e => setSetFbInstruction(e.target.value)}
                      disabled={setFbBusy || setFbExhausted || feedbackLocked
                        || recognition !== null}
                      placeholder="e.g. more behavioral signals, fewer balance aggregates"
                    />
                    <button
                      type="submit"
                      className="btn btn--primary"
                      disabled={setFbBusy || setFbExhausted || feedbackLocked || generating
                        || recognition !== null
                        || !setFbInstruction.trim()}
                    >
                      {setFbBusy
                        ? 'Regenerating…'
                        : `Regenerate with feedback · round ${Math.min(setFbRounds + 1, FEEDBACK_ROUNDS)} of ${FEEDBACK_ROUNDS}`}
                    </button>
                  </div>
                </div>
                {setFbExhausted ? (
                  <p className="hint" role="status">
                    Rounds exhausted. Approve, edit by hand, or restate the goal.
                  </p>
                ) : (
                  <p className="hint">
                    Applies to all sets: approved and selected features are kept; the rest
                    regenerate under your guidance. Recorded under your name. 3 rounds, then it
                    is back in your hands.
                  </p>
                )}
                {/* The recorded rounds go behind a COUNT-BEARING disclosure — "2 of 3 rounds
                    recorded" is a fact the human can act on; a bare chevron is not. Nothing is
                    dropped: every strip is still there, verbatim, one click away. */}
                {setFbRecords.length > 0 && (
                  <details className="round-history">
                    <summary className="tabular-nums">
                      {setFbRecords.length} of {FEEDBACK_ROUNDS} rounds recorded — what you
                      asked, and what each round kept
                    </summary>
                    <div className="setfb-records" role="status">
                      {setFbRecords.map(r => (
                        <p key={r.round} className="setfb-record">
                          {`Set feedback round ${r.round} of ${FEEDBACK_ROUNDS} · recorded · from user:${r.user} · "${r.instruction}" · kept ${r.kept} selected, replaced ${r.replaced}`}
                        </p>
                      ))}
                    </div>
                  </details>
                )}
              </form>
            )}
            {/* ── Narrowing the active set ──────────────────────────────────────────────────────
                Search and facets over metadata the engine already sent. There is no score and no
                re-ranking here: the order is still the engine's, and these controls only hide
                rows. Every chip carries the count it would leave, and every group says what its
                values were counted from. */}
            <div className="result-toolbar" role="search" aria-label="Narrow this set">
              <div className="field toolbar-search">
                <label htmlFor="wb-candidate-search">Search this set</label>
                <input
                  id="wb-candidate-search"
                  type="search"
                  value={candidateQuery}
                  onChange={e => setCandidateQuery(e.target.value)}
                  placeholder="name, description, or a column it derives from"
                />
              </div>
              {facetGroups.map(group => (
                <div
                  key={group.axis}
                  className="facet-group"
                  role="group"
                  aria-label={`${group.legend} — counted from ${group.source}`}
                >
                  <span className="micro-label">{group.legend}</span>
                  <div className="facet-chips">
                    {group.options.map(option => {
                      const on = filterTokens.includes(option.token)
                      return (
                        <button
                          key={option.token}
                          type="button"
                          className="facet-chip"
                          aria-pressed={on}
                          onClick={() => setFilterTokens(tokens => on
                            ? tokens.filter(t => t !== option.token)
                            : [...tokens, option.token])}
                        >
                          {option.label}{' '}
                          <span className="tabular-nums facet-n">{option.count}</span>
                        </button>
                      )
                    })}
                  </div>
                </div>
              ))}
              {/* Deliberately NOT a live region: this number changes on every keystroke, and a
                  polite announcement per character is noise, not access. It is plain text in
                  reading order, immediately before the list it describes. */}
              <p className="toolbar-count tabular-nums">
                Showing {filteredList.length} of {listCandidates.length}{' '}
                {listCandidates.length === 1 ? 'candidate' : 'candidates'}
                {multiSet && activeLens !== null ? ` in the ${lensLabel(activeLens)} set` : ''}.
                {narrowing && (
                  <>
                    {' '}
                    <button
                      type="button"
                      className="btn"
                      onClick={clearResultView}
                    >
                      Clear search and filters
                    </button>
                  </>
                )}
              </p>
            </div>
            <ul className="rows">
              {groupedList.map(({ heading, candidate: c }) => {
                const reg = registered[c.key]
                const gov = governed[c.key]
                const error = errors[c.key]
                const rawName = c.kind === 'generated' ? c.idea.name : c.name
                const displayName = rawName.trim() || 'unnamed draft'
                const sameNameVariants = c.kind === 'generated'
                  ? (generated ?? []).filter(other => other.idea.name === c.idea.name).length
                  : 0
                const variantContext = c.kind === 'generated' && sameNameVariants > 1
                  ? c.kept
                    ? 'earlier round'
                    : `${c.lenses[0] ?? 'unscoped'}; ${generationSourceLabel(c.idea)}`
                  : null
                // A4: the SERVER decides selectability. A card with an option-actions entry is
                // selectable only when create_contract is allowed; its first blocker's next
                // step becomes the disabled control's tooltip. Cards without an entry
                // (legacy/free-form, pre-B1) keep today's rule — the draft fold still gates.
                const actionsEntry = c.kind === 'generated' && c.idea.option_id
                  ? optionActions[c.idea.option_id]
                  : undefined
                const canSelect = actionsEntry
                  ? actionsEntry.allowed_actions.includes('create_contract')
                  : c.kind === 'generated' || c.name.trim() !== ''
                const blockedTitle = actionsEntry && !canSelect
                  ? actionsEntry.blocked_actions.create_contract?.[0]?.next_step
                    ?? 'not yet actionable'
                  : undefined
                const description = c.kind === 'generated' ? c.idea.description : c.description
                // D3 §UI-04 "can it be built": the SERVER's own verdict — plannable when
                // create_contract is allowed; else the first blocker's named next step.
                const buildability = actionsEntry
                  ? (actionsEntry.allowed_actions.includes('create_contract')
                    ? 'Plannable — a governed contract is available'
                    : actionsEntry.blocked_actions.create_contract?.[0]?.next_step ?? null)
                  : null
                // Review currency is its OWN axis (never folded into one green badge): derived
                // from the server's blocker codes, shown only for recipe-origin cards.
                const reviewNotCurrent = c.kind === 'generated'
                  && c.idea.generation_source === 'recipe'
                  && (actionsEntry?.blocked_actions.create_contract ?? [])
                    .some(b => b.code === 'RECIPE_REVIEW_NOT_CURRENT')
                const aggregation = c.kind === 'generated' ? c.idea.aggregation : c.recipe.aggregation
                const grain = c.kind === 'generated' ? c.idea.grain_table : c.recipe.grain_table
                const derives = c.kind === 'generated'
                  ? fmtPairs(c.idea.derives_pairs)
                  : c.recipe.derives_from.map(ref => `${c.snapshotSource}:${ref}`).join(', ') || 'none'
                const refine = refines[c.key] ?? EMPTY_REFINE
                const refineExhausted = refine.rounds >= FEEDBACK_ROUNDS
                return (
                  <Fragment key={c.key}>
                  {heading !== null && (
                    <li aria-hidden={false} role="presentation" className="row-group-heading"
                        style={{ listStyle: 'none', paddingTop: 10 }}>
                      <h4 style={{ margin: 0, fontSize: 13, color: 'var(--ink-soft)',
                                   textTransform: 'uppercase', letterSpacing: 0.4 }}>
                        {heading}
                      </h4>
                    </li>
                  )}
                  <li
                    className="row"
                    id={`wb-row-${c.key}`}
                    tabIndex={-1}
                    style={{ alignItems: 'flex-start' }}
                  >
                    {reg || gov ? (
                      <CheckGlyph />
                    ) : (
                      <input
                        type="checkbox"
                        aria-label={`Select ${displayName}${variantContext ? ` (${variantContext})` : ''}`}
                        title={blockedTitle}
                        checked={c.key in selected}
                        disabled={batchBusy || !canSelect}
                        onChange={() => toggleSelect(
                          // A kept row belongs to no current set: selecting it never stamps the
                          // viewed lens; its neutral origin drives the tray mix note.
                          c.key,
                          c.kind === 'generated' && multiSet && c.kept !== true
                            ? activeLens
                            : null)}
                        style={{ width: 18, height: 18, margin: 10, flex: 'none' }}
                      />
                    )}
                    <div style={{ display: 'grid', gap: 8, flex: 1, minWidth: 0, padding: '6px 0' }}>
                      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
                        <span className={c.kind === 'draft' ? 'mono' : undefined} style={{ fontWeight: 600 }}>
                          {displayName}
                        </span>
                        <span className="badge proposal">
                          {c.kind === 'generated' ? 'Proposal' : 'Draft'}
                        </span>
                        {c.kind === 'generated' && (
                          <span className="badge">{generationSourceLabel(c.idea)}</span>
                        )}
                        {c.kind === 'generated' && c.idea.candidate_status && (
                          <span className="badge">{c.idea.candidate_status}</span>
                        )}
                        {/* Honest stamp — SE-12: the tri-state decides the chip. A candidate the
                            backend marked NEEDS_EXTERNAL_VALIDATION must never wear the green
                            "design-checked" badge; it wears the amber checks-outstanding one, with
                            the count. Drafts skip the gauntlet, so they carry no stamp. */}
                        {c.kind === 'generated'
                          && c.idea.validation_status === 'NEEDS_EXTERNAL_VALIDATION' && (
                          <span className="badge stale">
                            needs data checks{c.idea.requirements?.length
                              ? ` (${c.idea.requirements.length})` : ''}
                          </span>
                        )}
                        {c.kind === 'generated' && c.idea.verification
                          && c.idea.validation_status !== 'NEEDS_EXTERNAL_VALIDATION' && (
                          /* The tone follows the STAMP. Since T2/T3 the server derives this
                             from the recipe's readiness as well as the gauntlet, so a card can
                             legitimately say UNVERIFIED — and a soft-ok chip over that word is
                             the same confidence-without-warrant the stamp exists to remove. */
                          <span className={c.idea.verification === 'UNVERIFIED'
                            ? 'badge stale' : 'badge ok'}>
                            {c.idea.verification.toLowerCase()}
                          </span>
                        )}
                        {reviewNotCurrent && (
                          <span className="badge stale">review not current</span>
                        )}
                        {/* Pinned through a whole-round regeneration. Registered rows skip the
                            chip: Registered is already their mark. */}
                        {c.kind === 'generated' && c.kept === true && !reg && !gov && (
                          <span className="badge">Kept</span>
                        )}
                        {/* The human approved an engine revision in round n. */}
                        {c.kind === 'generated' && refine.appliedRound !== null && (
                          <span className="badge revised">Revised · R{refine.appliedRound}</span>
                        )}
                        {/* Task 3 near-label critic: FLAG-ONLY. Only too_close renders — a warning
                            the human weighs, never a removal. no_finding is not a clearance (no
                            chip); abstain is honest absence. */}
                        {c.kind === 'generated' && c.idea.near_label_verdict === 'too_close' && (
                          <span className="badge stale">⚠ near label</span>
                        )}
                      </div>
                      <p style={{ color: 'var(--ink-soft)' }}>{description}</p>
                      {buildability && (
                        <p style={{ color: 'var(--ink-soft)', fontSize: 13 }} role="note">
                          {buildability}
                        </p>
                      )}
                      {c.kind === 'generated' && c.idea.rationale && (
                        <p style={{ color: 'var(--ink-soft)' }}>
                          {c.idea.generation_source === 'llm_intent'
                            ? "Model's rationale: " : 'Why: '}
                          {c.idea.rationale}
                        </p>
                      )}
                      {c.kind === 'generated' && c.idea.near_label_verdict === 'too_close'
                        && c.idea.near_label_rationale && (
                        <p style={{ color: 'var(--ink-soft)' }} role="note">
                          Near-label check: {c.idea.near_label_rationale}
                        </p>
                      )}
                      {/* SE-12 "Inputs and why": one row per typed operand role — the binding
                          the engine actually chose, its MEASURED authority, and whether Gate-1
                          confirmation is outstanding. Only engine/recipe candidates carry these;
                          an LLM free-form idea keeps its untyped derives list below. */}
                      {c.kind === 'generated' && (c.idea.input_role_bindings?.length ?? 0) > 0 && (
                        <ul aria-label="typed inputs" style={{ display: 'grid', gap: 2, margin: 0,
                            paddingLeft: 16, color: 'var(--ink-soft)', fontSize: 13 }}>
                          {c.idea.input_role_bindings!.map(binding => (
                            <li key={binding.role}>
                              <span style={{ fontWeight: 600 }}>{binding.role}</span>
                              {binding.ref && <> — <span className="mono">{binding.ref[1]}</span></>}
                              {binding.authority && <> · {binding.authority}</>}
                              {binding.confirmation_required && (
                                // UI-06: straight to the asset field-decision screen, focused
                                // on THIS field. Returning regenerates a fresh run — the card
                                // never self-approves its own metadata.
                                <a
                                  className="badge stale"
                                  style={{ marginLeft: 6 }}
                                  href={binding.ref
                                    ? `#asset?source=${encodeURIComponent(binding.ref[0])}`
                                      + `&object_ref=${encodeURIComponent(binding.ref[1])}`
                                    : '#governance'}
                                >
                                  needs confirmation →
                                </a>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                      {/* SE-12: the outstanding checks, task-first — what somebody DOES, with the
                          backend's own prose (which names the semantic origin) as the fine print. */}
                      {c.kind === 'generated' && (c.idea.requirements?.length ?? 0) > 0 && (
                        <ul aria-label="outstanding checks" style={{ display: 'grid', gap: 2,
                            margin: 0, paddingLeft: 16, color: 'var(--ink-soft)', fontSize: 13 }}>
                          {c.idea.requirements!.map(req => (
                            <li key={`${req.code}:${req.operand[1]}`}>
                              {requirementTask(req.code)}
                              {' — '}<span className="mono">{req.operand[1]}</span>
                              {req.detail && (
                                <span style={{ display: 'block', fontSize: 12 }}>{req.detail}</span>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                      {/* Task 4b emission policy: the untaken parameterisations, named on the
                          card — chosen value in brackets. Server populates only under its flag. */}
                      {c.kind === 'generated' && c.idea.param_alternatives && (
                        <p style={{ color: 'var(--ink-soft)' }}>
                          Also available — {c.idea.param_alternatives}
                        </p>
                      )}
                      {/* DRAFT FORMULA — per candidate, and deliberately NOT the checkbox above.
                          Selecting a candidate must never spend; asking for its formula is a
                          separate, explicit act with its own button, and it does not select.
                          Requires the opaque option_id on a v2 revision: without both, there is no
                          frozen candidate to author against. */}
                      {c.kind === 'generated' && c.idea.option_id && consideredRevisionId && (
                        <FormulaDraftAction
                          consideredRevisionId={consideredRevisionId}
                          optionId={c.idea.option_id}
                          candidateName={displayName}
                          onStateChange={onDraftStateChange}
                        />
                      )}
                      {/* D3 (UI-02): the audit drawer — the STORED decision record on demand,
                          by exact (revision, option) key. Only options with decision rows
                          (engine candidates on a v2 revision) offer it. */}
                      {c.kind === 'generated' && c.idea.option_id
                        && consideredRevisionId
                        && optionActions[c.idea.option_id] && (
                        <div>
                          <button
                            type="button"
                            className="btn"
                            aria-expanded={auditOpenFor === c.idea.option_id}
                            onClick={() => void toggleAudit(c.idea.option_id!)}
                          >
                            {auditOpenFor === c.idea.option_id
                              ? 'Hide decision record' : 'Decision record'}
                          </button>
                          {auditOpenFor === c.idea.option_id && (
                            <div aria-label="decision record" className="audit-drawer"
                                 style={{ marginTop: 8, padding: 12, fontSize: 13,
                                          border: '1px solid var(--line)', borderRadius: 6 }}>
                              {auditError && <p role="alert">{auditError}</p>}
                              {!auditError && !auditDetail && <p>Loading the stored record…</p>}
                              {auditDetail?.decision_record ? (
                                <AuditDrawerBody record={auditDetail.decision_record} />
                              ) : auditDetail && !auditError ? (
                                <p className="hint">
                                  No stored decision record — this option predates the
                                  decision store. Nothing is recomputed in its place.
                                </p>
                              ) : null}
                            </div>
                          )}
                        </div>
                      )}
                      {/* Exploring mode's honest asymmetry, stated on the card (intake spec
                          default): with no declared target the leakage screens cannot run for an
                          LLM-origin candidate. Presentation only — never a removal. */}
                      {c.kind === 'generated'
                        && intakeReading?.target_provenance === 'exploring'
                        && screenedTarget === null
                        && (c.idea.generation_source ?? 'llm_freeform') === 'llm_freeform' && (
                        <p className="hint" role="note">
                          No target declared — leakage unchecked. Declare a target to screen this
                          candidate.
                        </p>
                      )}
                      <dl className="kv">
                        <div>
                          <dt>derives from</dt>
                          <dd className="mono">{derives}</dd>
                        </div>
                        {aggregation && (
                          <div>
                            <dt>aggregation</dt>
                            <dd>{aggregation}</dd>
                          </div>
                        )}
                        {grain && (
                          <div>
                            <dt>grain</dt>
                            <dd>{grain}</dd>
                          </div>
                        )}
                        {c.kind === 'draft' && (
                          <div>
                            <dt>drafted against</dt>
                            <dd className="mono">{c.snapshotSource}</dd>
                          </div>
                        )}
                      </dl>
                      {c.kind === 'draft' && !reg && (
                        <div className="field" style={{ maxWidth: 380 }}>
                          <label htmlFor={`wb-name-${c.key}`}>Name</label>
                          <input
                            id={`wb-name-${c.key}`}
                            className="mono"
                            value={c.name}
                            onChange={e => renameDraft(c.key, e.target.value)}
                            placeholder="feature_name"
                          />
                          {!c.name.trim() && (
                            <p className="hint">Name this draft to select it for registration.</p>
                          )}
                        </div>
                      )}
                      {c.kind === 'draft' && <JoinPathDetails steps={c.recipe.join_path} />}
                      {error && (
                        <p className="error" role="alert">
                          {error}
                        </p>
                      )}
                      {/* Per-candidate feedback: generated rows only (a draft is the human's own
                          definition, revised by editing its line), never on registered or governed
                          rows (a minted contract is finalized; feedback would diverge from it). */}
                      {c.kind === 'generated' && !reg && !gov && (
                        <>
                          <div>
                            <button
                              type="button"
                              className="btn"
                              aria-expanded={refine.open}
                              aria-controls={`wb-refine-${c.key}`}
                              disabled={feedbackLocked}
                              onClick={() => toggleRefine(c.key)}
                            >
                              Give feedback
                            </button>
                          </div>
                          {refine.open && (
                            <form
                              id={`wb-refine-${c.key}`}
                              className="refine-box"
                              onSubmit={e => {
                                e.preventDefault()
                                void sendRefine(c)
                              }}
                            >
                              <div className="field">
                                <label htmlFor={`wb-refine-input-${c.key}`}>
                                  What should change
                                </label>
                                <input
                                  id={`wb-refine-input-${c.key}`}
                                  value={refine.instruction}
                                  onChange={e => {
                                    const value = e.target.value
                                    patchRefine(c.key, prev => ({ ...prev, instruction: value }))
                                  }}
                                  disabled={refine.busy || refineExhausted || feedbackLocked}
                                  placeholder="e.g. use a 30 day window"
                                />
                                <p className="hint" style={HELP_STYLE}>
                                  Your feedback runs the engine once, re-checks safety, and is
                                  recorded under your name. 3 rounds per candidate, then it is
                                  back in your hands.
                                </p>
                              </div>
                              <button
                                type="submit"
                                className="btn btn--primary"
                                disabled={refine.busy || refineExhausted || feedbackLocked
                                  || !refine.instruction.trim()}
                              >
                                {refine.busy
                                  ? 'Requesting revision…'
                                  : refineExhausted
                                    ? 'Rounds exhausted'
                                    : `Send feedback for one revision · round ${refine.rounds + 1} of ${FEEDBACK_ROUNDS}`}
                              </button>
                            </form>
                          )}
                          {refine.error && (
                            <p className="error" role="alert">
                              {refine.error}
                            </p>
                          )}
                          {refine.rejection && (
                            <p className="error" role="alert">
                              The safety gauntlet rejected this revision:{' '}
                              {refine.rejection.reason} ({rejectLabel(refine.rejection.code)}).
                              The round is consumed; the candidate is unchanged.
                            </p>
                          )}
                          {refine.pending && (
                            <div className="revision" role="status">
                              <div className="rev-meta">
                                <span className="badge revised">
                                  Revision · round {refine.pendingRound} of {FEEDBACK_ROUNDS}
                                </span>
                                <span className="rev-who">
                                  {`recorded · from user:${getSession().user} · "${refine.pendingInstruction}"`}
                                </span>
                              </div>
                              <div className="diff">
                                <DiffLine
                                  label="name"
                                  before={c.idea.name}
                                  after={refine.pending.name}
                                />
                                <DiffLine
                                  label="description"
                                  before={c.idea.description}
                                  after={refine.pending.description}
                                />
                                <DiffLine
                                  label="aggregation"
                                  before={c.idea.aggregation ?? 'none'}
                                  after={refine.pending.aggregation ?? 'none'}
                                />
                                <DiffLine
                                  label="derives"
                                  before={fmtPairs(c.idea.derives_pairs)}
                                  after={fmtPairs(refine.pending.derives_pairs)}
                                />
                              </div>
                              <p className="recheck">Re-checked after revision</p>
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 }}>
                                {/* Inert while the tray is confirming or a batch is in flight:
                                    the spec being written must not change under the approval. */}
                                <button
                                  type="button"
                                  className="btn btn--primary"
                                  disabled={feedbackLocked}
                                  onClick={() => approveRevision(c.key)}
                                >
                                  Approve revision
                                </button>
                                <button
                                  type="button"
                                  className="btn"
                                  disabled={feedbackLocked}
                                  onClick={() => revertRevision(c.key)}
                                >
                                  Revert to original
                                </button>
                              </div>
                            </div>
                          )}
                        </>
                      )}
                      {reg && (
                        <p
                          style={{
                            color: 'var(--ok)', fontWeight: 500,
                            display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8,
                          }}
                        >
                          <span>
                            Registered <span className="mono">{reg.id}</span>
                          </span>
                          {reg.freshness && (reg.freshness.fresh ? (
                            <span className="badge" style={OK_SOLID_CHIP_STYLE}>fresh</span>
                          ) : (
                            <span className="badge stale">
                              stale: {reg.freshness.stale_sources.join(', ')}
                            </span>
                          ))}
                        </p>
                      )}
                      {/* Governed mark, parallel to the registered one: a minted, versioned,
                          design-checked contract. Its own state, so no checkbox and no feedback. */}
                      {gov && (
                        <p style={{ color: 'var(--ok)', fontWeight: 500 }}>
                          Governed <span className="mono">{gov.contractId}</span> v{gov.version}
                          {' · DESIGN-CHECKED'}
                        </p>
                      )}
                    </div>
                  </li>
                  </Fragment>
                )
              })}
            </ul>
            {/* The list can be narrowed to nothing. That is a fact about the FILTER, never about
                the round: the counts above still stand, and the way back is one click. */}
            {groupedList.length === 0 && (
              <p className="empty" role="status">
                No candidate in this set matches the current search and filters.{' '}
                {listCandidates.length} {listCandidates.length === 1 ? 'candidate is' : 'candidates are'}{' '}
                here with them cleared.
              </p>
            )}
          </div>

          {/* ── The decision rail ─────────────────────────────────────────────────────────────
              What the next click will actually write, kept beside the comparison instead of at
              the end of the list. Nothing here is new governance: the same two confirmation
              arms, the same server-decided governability, the same per-item outcomes. */}
          <aside className="decision-rail" aria-label="Your decision">
            <div className="panel decision-card" data-tray={selectedCount > 0 || undefined}>
              <div className="decision-head">
                <h3>Your decision tray</h3>
                <span className="micro-label tabular-nums">
                  {selectedCount} selected
                </span>
              </div>
              {/* HOW MANY OF THE SELECTED ONES HAVE A FORMULA — the number that decides whether
                  the next step is cheap or expensive, shown BEFORE it is taken. Counted from the
                  per-candidate draft states the rows report upward, so the tray never asks the
                  draft API anything and selecting still costs nothing.

                  Only the SELECTED candidates are counted. A user who drafted six formulas while
                  browsing and selected two needs "2 selected · 1 ready", not a tally of everything
                  they looked at. */}
              {selectedCount > 0 && draftSummary !== null && (
                <p className="micro-label tabular-nums" style={{ margin: '4px 0 0' }}>
                  {draftSummary}
                </p>
              )}
              {confirmingBatch ? (
                <>
                  <p style={{ fontWeight: 500 }}>
                    This saves the selected candidates as IDEAS — browsable sketches recorded
                    under your name with their lineage. An idea is not a governed feature and
                    can never feed a model; governing happens through a contract.
                  </p>
                  <div className="decision-actions">
                    <button
                      type="button"
                      className="btn btn--proposal-confirm"
                      disabled={batchBusy}
                      onClick={() => void confirmRegistration()}
                    >
                      Save ideas
                    </button>
                    <button
                      type="button"
                      className="btn"
                      disabled={batchBusy}
                      onClick={() => setConfirmingBatch(false)}
                    >
                      Cancel
                    </button>
                  </div>
                </>
              ) : confirmingGovern ? (
                <>
                  <p style={{ fontWeight: 500 }}>
                    Governing runs the safety gauntlet and mints a signed contract per feature —
                    a design check, not a proof it predicts well.
                  </p>
                  <div className="decision-actions">
                    <button
                      type="button"
                      className="btn btn--primary"
                      disabled={governBusy}
                      onClick={() => void confirmGovern()}
                    >
                      Confirm govern
                    </button>
                    <button
                      type="button"
                      className="btn"
                      disabled={governBusy}
                      onClick={() => setConfirmingGovern(false)}
                    >
                      Cancel
                    </button>
                  </div>
                </>
              ) : selectedCount === 0 ? (
                // Honest absence: an empty tray states the structure of the decision and the
                // fact that nothing has been written, rather than showing a dead button.
                <p className="hint">
                  Nothing is selected yet. Tick candidates in any set, or take a whole set —
                  selection is reversible right up to your confirmation.
                </p>
              ) : (
                <>
                  {/* Where the picks came from, and the picks the current narrowing hides. */}
                  {mixNote !== null && <p className="hint">{mixNote}</p>}
                  {hiddenSelectedCount > 0 && (
                    <p className="hint" role="status">
                      {hiddenSelectedCount} of them{' '}
                      {hiddenSelectedCount === 1 ? 'is' : 'are'} hidden by the current search and
                      filters. They stay selected and will still be written.
                    </p>
                  )}
                  {/* What "Approve and register" WRITES — said before the click, not after. */}
                  <p className="hint">
                    Approve and register writes {selectedCount}{' '}
                    {selectedCount === 1
                      ? 'definition with its lineage'
                      : 'definitions with their lineage'}, under your name. It records what to
                    compute; it computes nothing and proves nothing about predictive value.
                  </p>
                  {/* Governability is the SERVER's decision, so the rail reports it and never
                      re-derives it. Saying which selections cannot be governed — and that the
                      rest still can — is the difference between a warning and a dead end. */}
                  <p className="hint">{governabilityNote}</p>
                  <div className="decision-actions">
                    {/* Govern the generated picks into signed contracts through the two-gate
                        flow. Shown only when a governing intent exists (from the last
                        considered-set call — generate or feedback) and at least one selected
                        candidate is a fresh generated one (governable). */}
                    {/* SELECT AND DRAFT, named for what it does. This runs `/contract/draft`,
                        whose FIRST act is to record a Gate-1 choice — so pressing it chooses, and a
                        label that said only "draft" would select behind the user's back. The
                        per-candidate "Draft formula" button on each row is the one that drafts
                        WITHOUT choosing; these are two different acts and read as two. */}
                    {intentId !== null && governableCount > 0 && (
                      <button
                        type="button"
                        className="btn"
                        title="Records your choice, then drafts and governs the specification"
                        onClick={() => setConfirmingGovern(true)}
                      >
                        Select and draft {governableCount}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn--primary"
                      onClick={() => setConfirmingBatch(true)}
                    >
                      Approve and register {selectedCount}{' '}
                      {selectedCount === 1 ? 'feature' : 'features'}
                    </button>
                  </div>
                </>
              )}
              {/* Per-item outcomes, counted. A partial batch shows BOTH numbers: hiding the
                  successes to lead with the failures is as dishonest as the reverse. Each
                  outcome also stays on its own row, which is where it can be acted on. */}
              {(settledRows > 0 || failedRows > 0) && (
                <p className="hint tabular-nums" role="status">
                  {settledRows > 0 && (
                    <>{settledRows} of this round&apos;s candidates{' '}
                      {settledRows === 1 ? 'is' : 'are'} saved or governed.</>
                  )}
                  {failedRows > 0 && (
                    <> {failedRows} could not be written — each one says why on its own row.</>
                  )}
                </p>
              )}
            </div>
            {/* The concept also drew a "What is known" card here (target screened / design
                checked / predictive value unknown). It is NOT built: all three claims are
                already stated above the list, and content rule 4 of the review is precisely
                that guidance must not be repeated in a second place with different words. The
                rail carries the DECISION — count, origins, consequence, governability,
                outcomes — and nothing that is already on screen. */}
          </aside>
        </div>
      )}

      {/* Phase 1B disposition lens: only when a SCOPED response carried dispositions and the
          intent_disposition_lens flag is on. Groups the recipe library by how the confirmed scope
          dispositioned each recipe, and keeps "show all buildable recipes" one click away. */}
      {dispositionLens && dispositions !== null && (
        <div className="panel" id="wb-disposition-lens">
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
            <h2>How your scope dispositioned the recipes</h2>
            <span className="micro-label tabular-nums">
              <span style={{ color: 'var(--accent)' }}>{dispositions.length}</span> recipes
            </span>
          </div>
          {DISPOSITION_GROUPS.map(group => {
            const recipes = dispositions.filter(d => d.final_disposition === group.key)
            if (recipes.length === 0) return null
            return (
              <div key={group.key} className="disposition-group" style={{ marginTop: 12 }}>
                <h3 style={{ margin: '0 0 8px' }}>
                  {group.heading}{' '}
                  <span className="micro-label tabular-nums">{recipes.length}</span>
                </h3>
                <ul className="rows">
                  {recipes.map(d => (
                    <li key={d.recipe_id} className="row" style={{ gap: 10, alignItems: 'baseline' }}>
                      <span className="mono" style={{ fontWeight: 600 }}>{d.recipe_id}</span>
                      <span className="hint">{dispositionReason(d)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )
          })}
          <div style={{ marginTop: 12 }}>
            <button
              type="button"
              className="btn"
              disabled={generating}
              onClick={() => void broadenScope()}
            >
              Show all buildable recipes
            </button>
          </div>
        </div>
      )}

      {/* Phase 2A: the deterministic presentation-priority ranking of the eligible recipes. Rendered
          only behind VITE_INTENT_RANKING, when a scoped response carried `ranking`. A DISTINCT
          presentation from the candidate cards: canonical order, an initial-view subset with a
          "Show all" expander, per-recipe rank reasons and (for held-back recipes) a separate
          "why not shown initially" stream, and the LLM recommendation as its own labelled band. */}
      {rankingUi && ranking !== null && (
        <div className="panel" id="wb-ranking">
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
            <h2>Recipes by priority</h2>
            <span className="micro-label tabular-nums">
              <span style={{ color: 'var(--accent)' }}>{rankedOrder.length}</span>{' '}
              {rankedOrder.length === 1 ? 'recipe' : 'recipes'}
            </span>
            {rankingVersion && <span className="micro-label">ranking {rankingVersion}</span>}
          </div>
          <p className="hint" style={{ marginTop: 4 }}>
            A deterministic presentation priority over the eligible recipes — a stable ordering, never
            a prediction of predictive value.
          </p>
          {/* The LLM "recommended starting set" is a SEPARATE band from the deterministic ranking: an
              advisory lens pick, clearly labelled and visually distinct, never merged into the ranked
              list below. Its own reasoning + caveat, verbatim from the backend. */}
          {recommendation !== null && (
            <div
              className="advice"
              data-band="recommended-starting-set"
              style={{ marginTop: 12 }}
            >
              <p>
                <strong>
                  Recommended starting set: {lensLabel(recommendation.recommended_lens)}.
                </strong>{' '}
                {recommendation.reasoning}
              </p>
              <p className="advice-caveat">Caveat: {recommendation.caveat}</p>
            </div>
          )}
          {/* The deterministic ranked list: the initial-view subset first. */}
          <ul className="rows">
            {rankedInitial.map(r => (
              <RankedRecipeRow key={r.recipe_id} recipe={r} warnings={signalWarnings?.[r.recipe_id]} />
            ))}
          </ul>
          {rankedRest.length > 0 && (
            <>
              <button
                type="button"
                className="btn"
                aria-expanded={showAllRanked}
                aria-controls="wb-ranking-rest"
                onClick={() => setShowAllRanked(open => !open)}
              >
                {showAllRanked ? 'Show fewer' : `Show all ${rankedOrder.length} recipes`}
              </button>
              {showAllRanked && (
                <ul className="rows" id="wb-ranking-rest">
                  {rankedRest.map(r => (
                    <RankedRecipeRow key={r.recipe_id} recipe={r} warnings={signalWarnings?.[r.recipe_id]} />
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}
    </section>
  )
}
