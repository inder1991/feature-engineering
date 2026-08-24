import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { FeatureExecutionScreen } from './FeatureExecutionScreen'
import * as api from '../api'

// S11's UI acceptance: *the buttons are the only client-side callers*, and *results sit above
// intake*. Both are claims about STRUCTURE rather than about one interaction, so most of this file
// is structural — a click test can show that a button works, and says nothing about the effect,
// interval or retry that might also be calling.

const PROPS = {
  artifactId: 'art-1',
  environmentId: 'hdfc-local',
  logicalGroupName: 'customer_txn_features',
  inventoryObservationId: 'obs-1',
  generationAuthorizationRevisionId: 'gar-1',
  checkSetHash: 'sha256:cs',
  goal: 'Predict churn for retail current accounts',
  targetMode: 'prediction',
  targetRef: 'hdfc::public.customers.churned_flag',
}

function stubReads() {
  vi.spyOn(api, 'verifyEligibility').mockResolvedValue({
    action: 'verify', allowed: true, blockers: [],
  })
  vi.spyOn(api, 'getArtifactCode').mockResolvedValue({
    artifact_id: 'art-1',
    environment_id: 'hdfc-local',
    logical_group_name: 'customer_txn_features',
    project_digest: 'sha256:project',
    files: [{ path: 'conf/base/catalog.yml', content: 'txn: {}\n' }],
    policy_realizations: [{ revision_id: 'rev-fx', occurrence_hash: 'occ-fx' }],
  })
  vi.spyOn(api, 'getPublicationStatus').mockResolvedValue({
    environment_id: 'hdfc-local',
    logical_group_name: 'customer_txn_features',
    blocked: false,
    blocking_attempt_id: null,
    blocking_outcome: null,
    detail: 'nothing is outstanding for this group',
  })
}

beforeEach(async () => {
  vi.restoreAllMocks()
  // `session.ts` keeps the dev session in a MODULE-LEVEL `let`, so a test that grants itself a role
  // leaks that identity into every test after it — and an identity-change test then sets a value
  // that is already current, sees no change, and fails for a reason that has nothing to do with the
  // component. Reset to the module's own default so each test starts from a known caller.
  const { setSession } = await import('../session')
  setSession({ user: 'dev', roles: ['data_owner'] })
})

// ══ ACCEPTANCE — the buttons are the ONLY client-side callers ═══════════════════════════════════
const SRC = join(__dirname, '..')

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) return sourceFiles(full)
    return /\.tsx?$/.test(entry.name) ? [full] : []
  })
}

describe('the two executing calls have exactly one caller each', () => {
  // Everything that leaves the browser and touches a cluster. The reads are deliberately not here:
  // they change nothing, which is why they are safe in an effect.
  const EXECUTING = ['requestVerification', 'requestPublication'] as const

  it.each(EXECUTING)('%s is named in exactly one screen, and it is this one', (call) => {
    const callers = sourceFiles(SRC).filter((file) => {
      if (file.endsWith('api.ts') || /\.test\.tsx?$/.test(file)) return false
      return new RegExp(`\\b${call}\\s*\\(`).test(readFileSync(file, 'utf8'))
    })
    expect(callers.map((f) => f.replace(SRC, ''))).toEqual(['/screens/FeatureExecutionScreen.tsx'])
  })

  it.each(EXECUTING)('%s appears inside an onClick handler, never an effect', (call) => {
    const text = readFileSync(join(SRC, 'screens/FeatureExecutionScreen.tsx'), 'utf8')
    // The handler that owns it, and the effect that must not.
    const handler = call === 'requestVerification' ? 'onVerify' : 'onPublish'
    const handlerBody = text.slice(text.indexOf(`async function ${handler}`))
    expect(handlerBody).toContain(call)

    const effect = text.slice(text.indexOf('useEffect('), text.indexOf('const stage'))
    expect(effect).not.toContain(call)
  })

  it('has no interval, timeout or polling loop at all', () => {
    // A poll that re-verified would run a cluster job every few seconds with nobody asking.
    const text = readFileSync(join(SRC, 'screens/FeatureExecutionScreen.tsx'), 'utf8')
    expect(text).not.toMatch(/setInterval|setTimeout|requestAnimationFrame/)
  })

  it('binds each executing call to a button, not to a link or a form submit', async () => {
    stubReads()
    render(<FeatureExecutionScreen {...PROPS} />)
    await waitFor(() => expect(api.verifyEligibility).toHaveBeenCalled())

    expect(screen.getByRole('button', { name: /verify in sandbox/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /publish to sandbox/i })).toBeTruthy()
  })
})

// ══ ACCEPTANCE — results sit ABOVE intake ══════════════════════════════════════════════════════
it('renders Results before Actions in the document', async () => {
  stubReads()
  const { container } = render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(api.getArtifactCode).toHaveBeenCalled())

  const results = screen.getByRole('region', { name: 'Results' })
  const actions = screen.getByRole('region', { name: 'Actions' })
  // DOCUMENT_POSITION_FOLLOWING: `actions` comes after `results`. A form on top would push the
  // answer below the fold on the one screen where the answer is the point.
  expect(results.compareDocumentPosition(actions) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(container.querySelector('.workspace-header')).toBeTruthy()
})

// ══ goal · target · stage · output, at the top ═════════════════════════════════════════════════
it('shows goal, target, stage and output above everything else', async () => {
  stubReads()
  render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(api.getArtifactCode).toHaveBeenCalled())

  expect(screen.getByText('Predict churn for retail current accounts')).toBeTruthy()
  expect(screen.getByText('hdfc::public.customers.churned_flag')).toBeTruthy()
  expect(screen.getByTestId('stage').textContent).toBe('generated')
  expect(screen.getByText(/customer_txn_features in hdfc-local/)).toBeTruthy()
})

it('says an exploration build HAS no target rather than showing an empty box', async () => {
  stubReads()
  render(<FeatureExecutionScreen {...PROPS} targetMode="exploration" targetRef={null} />)
  await waitFor(() => expect(api.verifyEligibility).toHaveBeenCalled())

  expect(screen.getByText(/no target — this is an exploration build/)).toBeTruthy()
})

// ══ the screen holds no policy ═════════════════════════════════════════════════════════════════
it("renders each blocker with the SERVER's reason, never one of its own", async () => {
  vi.spyOn(api, 'verifyEligibility').mockResolvedValue({
    action: 'verify',
    allowed: false,
    blockers: [{
      code: 'ARTIFACT_NOT_SERVABLE',
      reason: "The sealed artifact's subgraph check REFUSED it",
    }],
  })
  vi.spyOn(api, 'getArtifactCode').mockRejectedValue(
    new api.ApiError(409, 'the subgraph findings are on the artifact'))
  vi.spyOn(api, 'getPublicationStatus').mockResolvedValue({
    environment_id: 'hdfc-local', logical_group_name: 'customer_txn_features',
    blocked: false, blocking_attempt_id: null, blocking_outcome: null, detail: 'nothing',
  })

  render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(screen.getByText('ARTIFACT_NOT_SERVABLE')).toBeTruthy())
  expect(screen.getByText(/subgraph check REFUSED it/)).toBeTruthy()
  // Verify is disabled: the server said not allowed, and nothing here re-derives that.
  expect(screen.getByRole('button', { name: /verify in sandbox/i })
    .hasAttribute('disabled')).toBe(true)
})

it('calls no executing endpoint on mount', async () => {
  stubReads()
  const verify = vi.spyOn(api, 'requestVerification')
  const publish = vi.spyOn(api, 'requestPublication')

  render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(api.getPublicationStatus).toHaveBeenCalled())

  expect(verify).not.toHaveBeenCalled()
  expect(publish).not.toHaveBeenCalled()
})

it('verifies only when the button is pressed', async () => {
  stubReads()
  // The v2 shapes (§9.0): the POST answers with the durable REQUEST id, and the poll reads the
  // request's status with the legacy attempt fields as honest nulls.
  const verify = vi.spyOn(api, 'requestVerification').mockResolvedValue({
    request_id: 'vfr-1', created: true, sealed_artifact_id: 'art-1', detail: 'recorded',
  })
  const poll = vi.spyOn(api, 'getVerificationResult').mockResolvedValue({
    request_id: 'vfr-1', execution_hash: null, sealed_artifact_id: 'art-1',
    status: 'REQUESTED', stage_label: 'Queued — the durable worker will execute it',
    terminal: false, findings: [], failure_reason: null,
    attempt: null, staging_path: null, verified_output: null,
  })

  render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(api.verifyEligibility).toHaveBeenCalled())
  expect(verify).not.toHaveBeenCalled()

  await userEvent.click(screen.getByRole('button', { name: /verify in sandbox/i }))
  await waitFor(() => expect(verify).toHaveBeenCalledTimes(1))
  // The poll is keyed on the REQUEST id the POST returned — the read path the §9.0 rewrite
  // orphaned until the run-spine session caught it (the old code passed undefined here).
  expect(poll).toHaveBeenCalledWith('vfr-1')
  // The body carries no authorization and no environment — the route forbids both.
  expect(verify.mock.calls[0][0]).toEqual({
    sealed_artifact_id: 'art-1', check_set_hash: 'sha256:cs',
    inventory_observation_id: 'obs-1', attempt: 1,
  })
  expect(await screen.findByText(/a worker is still running this attempt/)).toBeTruthy()
})

it('publish stays disabled without a capability attestation', async () => {
  stubReads()
  render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(api.verifyEligibility).toHaveBeenCalled())

  // Publication REQUIRES a capability and verification must not — the asymmetry, at the control.
  expect(screen.getByRole('button', { name: /publish to sandbox/i })
    .hasAttribute('disabled')).toBe(true)
})

it('shows policy provenance for the governed realizations that produced the number', async () => {
  stubReads()
  render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(api.getArtifactCode).toHaveBeenCalled())

  expect(screen.getByText('Governed policies applied')).toBeTruthy()
  expect(screen.getByText('rev-fx')).toBeTruthy()
  expect(screen.getByText('occ-fx')).toBeTruthy()
})


// ══ READ SCOPE is an effect dependency ════════════════════════════════════════════════════════
// The second gap live testing found: every read here is read-scoped — `verifyEligibility` derives
// EXECUTION_AUTHORITY_UNMET from the caller's role claims, `getArtifactCode` is confirmer-gated —
// and the effect keyed on the URL alone, so granting yourself a role in the session bar left the
// original 403 frozen on screen. `session.ts` states the rule in as many words: "a result fetched
// under one identity is not a valid answer under another."
it('re-fetches when the session identity changes, not only when the URL does', async () => {
  const { setSession } = await import('../session')
  stubReads()
  render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(api.verifyEligibility).toHaveBeenCalledTimes(1))

  // Same URL, same props — only the caller's claims moved.
  setSession({ user: 'dev', roles: ['data_owner', 'platform-admin'] })
  await waitFor(() => expect(api.verifyEligibility).toHaveBeenCalledTimes(2))
})

it("clears the previous identity's refusal when the re-fetch succeeds", async () => {
  // The third defect live testing found: the re-fetch worked, but the banner from the FIRST
  // identity stayed on screen — reading as a fresh verdict about a request that had just
  // succeeded. A stale error is worse than no error.
  const { setSession } = await import('../session')
  vi.spyOn(api, 'getPublicationStatus').mockResolvedValue({
    environment_id: 'hdfc-local', logical_group_name: 'customer_txn_features',
    blocked: false, blocking_attempt_id: null, blocking_outcome: null, detail: 'nothing',
  })
  vi.spyOn(api, 'getArtifactCode').mockRejectedValue(
    new api.ApiError(403, 'requires the platform-admin role'))
  vi.spyOn(api, 'verifyEligibility').mockRejectedValue(
    new api.ApiError(403, 'requires the platform-admin role'))

  const { container } = render(<FeatureExecutionScreen {...PROPS} />)
  // Two banners carry it: the eligibility error and the code-view error.
  await waitFor(() => expect(container.querySelectorAll('.error')).toHaveLength(2))

  // Same screen, better claims — and now both reads succeed.
  vi.spyOn(api, 'verifyEligibility').mockResolvedValue({
    action: 'verify', allowed: true, blockers: [] })
  vi.spyOn(api, 'getArtifactCode').mockResolvedValue({
    artifact_id: 'art-1', environment_id: 'hdfc-local',
    logical_group_name: 'customer_txn_features', project_digest: 'sha256:project',
    files: [], policy_realizations: [],
  })
  setSession({ user: 'dev', roles: ['data_owner', 'platform-admin'] })

  await waitFor(() => expect(api.verifyEligibility).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(container.querySelectorAll('.error')).toHaveLength(0))
})


it('names the identity in the effect dependencies', () => {
  // Structural as well as behavioural: the behavioural test above passes if the component happens
  // to re-render for another reason, and the rule is about the DEPENDENCY.
  const text = readFileSync(join(SRC, 'screens/FeatureExecutionScreen.tsx'), 'utf8')
  expect(text).toMatch(/useIdentityKey\(\)/)
  const deps = text.slice(text.lastIndexOf('}, ['), text.lastIndexOf('}, [') + 140)
  expect(deps).toContain('identity')
})

// ══ REACHABILITY — the gap this file did not close the first time ══════════════════════════════
// Written after a deploy showed the screen's strings were ABSENT from the built bundle: the route
// and its icon existed, `nav.ts` parsed the hash, every test above passed — and `App.tsx` never
// rendered the component, so the whole screen was dead code. A component test cannot see that; only
// a test that looks at the thing doing the routing can.
describe('the screen is reachable from the app', () => {
  const APP = readFileSync(join(SRC, 'App.tsx'), 'utf8')

  it('App.tsx imports and renders FeatureExecutionScreen', () => {
    expect(APP).toMatch(/import\s*\{\s*FeatureExecutionScreen\s*\}/)
    expect(APP).toMatch(/<FeatureExecutionScreen/)
  })

  it('renders it behind its own flag, like every other detail sheet', () => {
    expect(APP).toMatch(/route === 'feature-execution' && featureExecutionEnabled\(\)/)
  })

  it.each([
    ['materialization', 'MaterializationRunScreen'],
    ['feature-execution', 'FeatureExecutionScreen'],
  ])('every flagged route in nav.ts has a renderer: %s', (route, component) => {
    // Generalised deliberately: the defect was a route that parsed and rendered nothing, and
    // pinning only the new one would leave the next addition free to repeat it.
    const NAV = readFileSync(join(SRC, 'nav.ts'), 'utf8')
    expect(NAV).toContain(`'${route}'`)
    expect(APP).toContain(`<${component}`)
  })
})

// ══ AN UNCERTAIN PUBLICATION IS NEVER SHOWN AS A PUBLISHED ONE ═════════════════════════════════
function drawWithPublication(over: Partial<api.PublicationStatus>) {
  stubReads()
  vi.spyOn(api, 'getPublicationStatus').mockResolvedValue({
    environment_id: 'hdfc-local',
    logical_group_name: 'customer_txn_features',
    blocked: false,
    blocking_attempt_id: null,
    blocking_outcome: null,
    detail: 'nothing is outstanding for this group',
    ...over,
  })
  render(<FeatureExecutionScreen {...PROPS} />)
}

it('SHOWS AN UNRECONCILED ATTEMPT AS UNCERTAIN, NOT AS PUBLISHED', async () => {
  // Found by review, and the worst kind of bug this screen could have: the ONE state meaning
  // "nobody knows whether the swap landed" was mapped onto the word for success. An operator
  // reading it would believe the feature was live and stop looking.
  drawWithPublication({
    blocked: true,
    blocking_attempt_id: 'pubatt-1',
    blocking_outcome: 'unknown_reconciliation_required',
    detail: 'an unreconciled attempt is outstanding: nobody knows whether its swap landed',
  })

  await waitFor(() =>
    expect(screen.getByTestId('stage').textContent).toBe('publication_uncertain'))
  // An ALERT: this is the state a person has to resolve, and nothing else will.
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('nobody knows whether its swap landed')
})

it('distinguishes an in-flight publication from an uncertain one', async () => {
  // STARTED and UNKNOWN_RECONCILIATION_REQUIRED both block a retry, but they are different facts:
  // one is progress, one needs a human. Collapsing them loses the only distinction that matters.
  drawWithPublication({
    blocked: true, blocking_attempt_id: 'pubatt-2', blocking_outcome: 'started',
    detail: 'an attempt is in flight',
  })

  await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('publishing'))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('NEVER CLAIMS PUBLISHED when no attempt is outstanding, because it is not told that', async () => {
  // `blocked: false` covers never-attempted, succeeded and failed alike. The endpoint reports
  // blocking attempts, not the active revision, so this screen cannot honestly say "published" —
  // and says exactly that rather than implying the feature is live.
  drawWithPublication({})

  expect(await screen.findByText(/not the same as published/)).toBeInTheDocument()
  expect(screen.getByTestId('stage').textContent).not.toBe('published')
})
