// BRIDGE DEMAND — what governed planning needed and did not have.
//
// The queue above this panel answers "which crossings has somebody PROPOSED?". This answers the
// question that funds them: which crossings did the platform actually need, how often, and for how
// many separate questions. Until this panel that record existed only in a table.
//
// ── THREE QUEUES, THREE DIFFERENT PIECES OF WORK, NEVER ONE LIST ─────────────────────────────────
//
//   1. BRIDGE DEMAND    — the frontier could not cross here AT ALL. Somebody must sanction a
//                         crossing between two catalogs. There is a far catalog that demonstrably
//                         does this join, and the endpoints name it.
//   2. REALIZATION GAPS — the crossing IS sanctioned and has no executable realization, so the
//                         hop's cardinality is unknown and no measure can be costed over it. That
//                         is a data-model question, not a permissions one.
//   3. PLANNER CAPACITY — the search ran out of budget. Nobody is missing a bridge; this is
//                         operational, and it carries no propose affordance because there is
//                         nothing here for a reviewer to decide.
//
// Fusing them would put "sanction a crossing", "build a realization" and "raise a budget" in one
// list addressed to the wrong person two-thirds of the time.
//
// ── THE THREE COUNTS ARE THREE QUESTIONS ─────────────────────────────────────────────────────────
// `demand_rows` counts planning attempts that filed this demand — one observation can file two
// rows, so it is never a count of features or of people. `distinct_intents` counts the separate
// questions somebody asked; `distinct_runs` counts the runs. They are shown TOGETHER, because five
// runs of one question is one team asking once and a queue that ranks it as five ranks noise.
//
// ── WHAT THIS PANEL DOES NOT SAY ─────────────────────────────────────────────────────────────────
// Never "blocked": a demand row is not a failure and nothing is stuck — the platform served
// whatever it could and recorded what it could not. Never "approved" for anything below VERIFIED.
// And no propose button: bridges are proposed by INGESTION from declared join evidence and
// confirmed in the queue above; there is no operator propose surface in the API to link to, so this
// panel names the endpoints and says plainly what would close the gap rather than offering a
// control that does not exist.
import { useCallback, useEffect, useState } from 'react'
import { ApiError, getBridgeDemand } from '../api'
import type { BridgeDemandGroup, BridgeDemandResp } from '../api'

type QueueName = 'bridge_demand' | 'realization_gap' | 'planner_capacity'

interface QueueCopy {
  heading: string
  about: string
  // What a reviewer would DO about a row in this queue. Absent for planner capacity: there is
  // nothing here for a reviewer to decide.
  next: string
  empty: string
}

const QUEUES: Array<{ name: QueueName } & QueueCopy> = [
  {
    name: 'bridge_demand',
    heading: 'Bridge demand',
    about: 'Governed planning could not cross between catalogs here. Another catalog '
      + 'demonstrably performs this join, so a sanctioned bridge between the endpoints below '
      + 'would let these roll-ups complete.',
    next: 'A bridge for these endpoints is proposed by catalog ingestion from declared join '
      + 'evidence, and is confirmed in the entity-bridge decisions above. Nothing here has been '
      + 'proposed yet.',
    empty: 'No bridge demand recorded yet — rows appear when governed cross-catalog planning '
      + 'cannot complete a roll-up.',
  },
  {
    name: 'realization_gap',
    heading: 'Realization gaps',
    about: 'Data-model gap — no catalog realizes this roll-up. The crossing is sanctioned; what '
      + 'is missing is an executable realization, without which the hop’s cardinality is '
      + 'unknown and no measure can be costed over it.',
    next: 'Closing one of these means building the realization in the position catalog below, or '
      + 'attaching one that already exists.',
    empty: 'No realization gaps recorded yet — rows appear when a sanctioned crossing has no '
      + 'executable realization behind it.',
  },
  {
    name: 'planner_capacity',
    heading: 'Planner capacity',
    about: 'Planner budget — operational. The search reached its bridge or frontier limit '
      + 'before it finished. Nobody is missing a crossing here.',
    next: '',
    empty: 'No planner-capacity records yet — rows appear when a governed search reaches its '
      + 'budget.',
  },
]

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}

// The crossing, in the direction a roll-up travels: `account ← transaction` reads "accounts, rolled
// up from transactions". A capacity row names no crossing (its hop identity is dropped by ruling),
// so it says so instead of rendering an arrow between two empty strings.
function crossingLabel(group: BridgeDemandGroup): string {
  if (!group.from_entity && !group.to_entity) {
    return group.relationship_id || 'No single crossing — a whole search ran out of budget'
  }
  return `${group.to_entity || '?'} ← ${group.from_entity || '?'}`
}

// Which lane RECORDED these rows, said in words rather than left as a code. Both lanes observe the
// same platform, so a row seen by both is not two demands.
function modeLabel(group: { live_observations: number; telemetry_observations: number }): string {
  const seen: string[] = []
  if (group.live_observations > 0) seen.push(`${group.live_observations} live`)
  if (group.telemetry_observations > 0) {
    seen.push(`${group.telemetry_observations} telemetry`)
  }
  return seen.length > 0 ? `Recorded by: ${seen.join(', ')}` : 'Recorded by: not attributed'
}

// The sample the store built: the far realizer's key column first, then the near-side anchor, each
// qualified by the catalog that makes it useful. Absent rather than empty when there is none —
// a capacity row names no endpoints, and an "Endpoints:" label with nothing after it says nothing.
function Endpoints({ refs }: { refs: string[] }) {
  if (refs.length === 0) return null
  return (
    <p className="hint bd-endpoints" data-testid="bd-endpoints">
      Endpoints:{' '}
      {refs.map((ref, index) => (
        <span key={ref}>
          {index > 0 ? ', ' : ''}
          <span className="mono">{ref}</span>
        </span>
      ))}
    </p>
  )
}

function DemandGroup({ group, queue }: { group: BridgeDemandGroup; queue: QueueName }) {
  return (
    <li className="row bd-group" data-testid={`bd-${queue}-group`}>
      <div className="bd-head">
        <span className="bd-crossing">{crossingLabel(group)}</span>
        {group.relationship_id && group.from_entity && (
          <span className="mono hint bd-rel">{group.relationship_id}</span>
        )}
      </div>
      {/* Runs beside questions rather than instead of them: a retry storm must be visible AS a
          retry storm, not read as demand. */}
      <p className="bd-counts" data-testid="bd-counts">
        <b>{plural(group.demand_rows, 'planning attempt', 'planning attempts')}</b> could not
        complete here · {plural(group.distinct_intents, 'question asked', 'questions asked')} ·{' '}
        {plural(group.distinct_runs, 'run', 'runs')}
      </p>
      <p className="hint bd-mode" data-testid="bd-mode">
        {modeLabel(group)} · {group.recent_observations} in the last 7 days,{' '}
        {group.historical_observations} older
      </p>
      <Endpoints refs={group.suggested_endpoints} />
      {group.nested.length > 0 && (
        <ul className="bd-positions">
          {group.nested.map(position => (
            <li
              key={`${position.position_catalog}:${position.position_table_ref}`}
              className="bd-position"
              data-testid="bd-position"
            >
              <span className="mono">
                {position.position_catalog || 'no catalog recorded'}
                {position.position_table_ref ? ` · ${position.position_table_ref}` : ''}
              </span>{' '}
              <span className="tabular-nums">
                {plural(position.demand_rows, 'attempt', 'attempts')},{' '}
                {plural(position.distinct_intents, 'question', 'questions')}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

// The container is "Planning demand", NOT "Bridge demand": the first of the three queues inside it
// is called Bridge demand, and a section whose heading repeats one of its own children's makes the
// outline ambiguous to anyone navigating by heading. The route keeps the domain name.
export function BridgeDemandPanel() {
  const [demand, setDemand] = useState<BridgeDemandResp | null>(null)
  const [notice, setNotice] = useState('')
  const [status, setStatus] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      setDemand(await getBridgeDemand())
    } catch (e) {
      setStatus(e instanceof ApiError ? e.status : null)
      setNotice(e instanceof ApiError ? e.detail : String(e))
    }
  }, [])

  useEffect(() => { void load() }, [load])

  // NOT AN ERROR — the same convention the queue above uses. This panel cannot check the caller's
  // role (this client holds no session claims, and inventing a check would be a second, drifting
  // authority), so it reacts to the 403 the server actually sent and quotes the server's own
  // sentence rather than paraphrasing the rule. A red alert would say something is broken.
  if (status === 403) {
    return (
      <section className="adg-section bd" data-testid="bridge-demand">
        <h2>Planning demand</h2>
        <p role="status" className="callout bd-not-yours" data-testid="bd-not-yours">
          This record is not open to your role — the server said: “{notice}”. Nothing is wrong and
          nothing here is waiting on you.
        </p>
      </section>
    )
  }

  // A FAILED READ MUST SAY SO, and must not read as an empty queue: "nobody needed a crossing" and
  // "we could not look" are different claims, and only one of them is good news.
  if (!demand) {
    if (!notice) return null
    return (
      <section className="adg-section bd" data-testid="bridge-demand">
        <h2>Planning demand</h2>
        <p role="status" className="callout callout--warn" data-testid="bd-notice">{notice}</p>
        <p className="hint">
          The demand record could not be read, so nothing here can be trusted to be complete. An
          empty list below would not mean nothing was recorded.
        </p>
      </section>
    )
  }

  const summary = demand.resolution_summary
  const rateText = (rate: number): string => `${Math.round(rate * 100)}%`
  const byOrigin = summary.by_origin
    .map(row => `${row.definition_origin} ${rateText(row.resolution_rate)}`)
    .join(' · ')

  return (
    <section className="adg-section bd" data-testid="bridge-demand">
      <h2>Planning demand</h2>
      <p className="hint bd-about">
        What governed planning needed and did not have. Every governed planning request leaves a
        record, whatever it did — so this is a measurement of the platform, not a list of faults.
      </p>
      {/* THE SUMMARY IS A DENOMINATOR, not a score. It says how much planning was recorded and how
          much of it resolved, per origin, so the queues below are read against something. */}
      <p className="bd-summary" data-testid="bd-summary">
        {plural(summary.totals.observations, 'planning request recorded',
          'planning requests recorded')} ·{' '}
        {summary.totals.resolved} resolved ({rateText(summary.totals.resolution_rate)})
        {byOrigin && <> · by origin: {byOrigin}</>}
      </p>

      {QUEUES.map(({ name, heading, about, next, empty }) => {
        const groups = demand.queues[name] ?? []
        return (
          <section className="bd-queue" key={name} data-testid={`bd-queue-${name}`}>
            <h3>
              {heading} <span className="tabular-nums">{groups.length}</span>
            </h3>
            <p className="hint bd-queue-about">{about}</p>
            {groups.length === 0 && (
              <p className="empty" role="status" data-testid={`bd-empty-${name}`}>{empty}</p>
            )}
            {groups.length > 0 && (
              <>
                <ul className="rows bd-groups">
                  {groups.map(group => (
                    <DemandGroup
                      key={`${group.relationship_id}:${group.from_entity}:${group.to_entity}`}
                      group={group}
                      queue={name}
                    />
                  ))}
                </ul>
                {next && (
                  <p className="hint bd-next" data-testid={`bd-next-${name}`}>{next}</p>
                )}
              </>
            )}
          </section>
        )
      })}

      {demand.next_cursor && (
        <p className="hint" data-testid="bd-truncated">
          More crossings are recorded than this page holds (it shows up to {demand.limit}).
        </p>
      )}
    </section>
  )
}
