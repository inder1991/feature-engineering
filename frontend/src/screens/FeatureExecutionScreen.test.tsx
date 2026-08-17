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

beforeEach(() => {
  vi.restoreAllMocks()
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
  const verify = vi.spyOn(api, 'requestVerification').mockResolvedValue({
    execution_hash: 'exec-1', sealed_artifact_id: 'art-1', attempt: 1,
    staging_path: 'hdfs://nn/staging/gar-1/attempt=1', detail: 'recorded',
  })
  vi.spyOn(api, 'getVerificationResult').mockResolvedValue({
    execution_hash: 'exec-1', sealed_artifact_id: 'art-1', attempt: 1,
    staging_path: 'hdfs://nn/staging/gar-1/attempt=1', started_at: 't0',
    verified_output: null,
  })

  render(<FeatureExecutionScreen {...PROPS} />)
  await waitFor(() => expect(api.verifyEligibility).toHaveBeenCalled())
  expect(verify).not.toHaveBeenCalled()

  await userEvent.click(screen.getByRole('button', { name: /verify in sandbox/i }))
  await waitFor(() => expect(verify).toHaveBeenCalledTimes(1))
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
