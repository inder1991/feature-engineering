import { useEffect, useState } from 'react'
import {
  ApiError,
  type ArtifactCode,
  type ExecutionBlocker,
  type PublicationStatus,
  type VerificationResult,
  getArtifactCode,
  getPublicationStatus,
  getVerificationResult,
  requestPublication,
  requestVerification,
  verifyEligibility,
} from '../api'
import { useIdentityKey } from '../session'

// S11 — the first user-reachable generation surface: what this feature is FOR, what it would run,
// and the two actions that reach a cluster.
//
// **THE BUTTONS ARE THE ONLY THING THAT VERIFIES OR PUBLISHES.** Not a convention — an acceptance
// clause, and the reason is that both actions leave this browser and touch a cluster. Verification
// executes generated code; publication makes a number visible to everyone downstream. So
// `requestVerification` and `requestPublication` appear in exactly one place each, inside an
// onClick, and nowhere in a `useEffect`, an interval or a retry. A polling loop that re-verified
// would run a cluster job every few seconds with nobody asking; a "refresh" that republished would
// be worse. The reads (`getVerificationResult`, `getPublicationStatus`, `verifyEligibility`,
// `getArtifactCode`) are safe in effects precisely because they change nothing, and the split
// between the two sets is the whole design of this file.
//
// **RESULTS SIT ABOVE INTAKE.** What happened is what an operator came here to see; the form that
// asks for another attempt is what they use afterwards. A form on top pushes the answer below the
// fold on the one screen where the answer is the point.
//
// **THIS SCREEN HOLDS NO POLICY.** Every blocker is rendered with the sentence the SERVER sent —
// they come from the same disposition table the corpus report reads — so a code cannot be explained
// one way here and another way there. Nothing derives an "allowed" from parts, because the server
// already decided and a second derivation would be a second answer.
//
// **HONEST ABSENCE.** `verified_output: null` means a worker has not finished, not that something
// failed. `target_ref: null` under an exploration build means there IS no target, and is rendered as
// that rather than as an empty box. No invented owners, no invented timestamps.

interface Props {
  artifactId: string
  environmentId: string
  logicalGroupName: string
  inventoryObservationId: string
  generationAuthorizationRevisionId: string
  checkSetHash: string
  // What this build is FOR, and what it predicts. `null` target under an exploration build.
  goal: string
  targetMode: string
  targetRef: string | null
}

// The stage a feature has reached. Derived from what the server returned — never stored, and never
// a fifth value invented to cover a gap.
type Stage = 'generated' | 'verifying' | 'verified' | 'published'

function BlockerList({ blockers }: { blockers: ExecutionBlocker[] }) {
  // Each code with the server's own reason. A code alone sends everyone to the same shrug.
  return (
    <ul className="blockers">
      {blockers.map((blocker) => (
        <li key={blocker.code}>
          <code>{blocker.code}</code>
          <span>{blocker.reason}</span>
        </li>
      ))}
    </ul>
  )
}

export function FeatureExecutionScreen(props: Props) {
  const {
    artifactId, environmentId, logicalGroupName, inventoryObservationId,
    generationAuthorizationRevisionId, checkSetHash, goal, targetMode, targetRef,
  } = props

  const [code, setCode] = useState<ArtifactCode | null>(null)
  const [codeError, setCodeError] = useState<string | null>(null)
  const [eligibility, setEligibility] = useState<ExecutionBlocker[] | null>(null)
  const [eligible, setEligible] = useState(false)
  const [verification, setVerification] = useState<VerificationResult | null>(null)
  const [publication, setPublication] = useState<PublicationStatus | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(1)
  const [capability, setCapability] = useState('')
  // READ SCOPE IS AN EFFECT DEPENDENCY, not just the URL. `session.ts` states the rule outright:
  // "a result fetched under one identity is not a valid answer under another." Every read below is
  // read-scoped — `verifyEligibility` returns EXECUTION_AUTHORITY_UNMET from the caller's role
  // claims, and `getArtifactCode` is confirmer-gated — so a screen keyed on the URL alone shows a
  // refusal from the identity it first loaded under and never re-asks. Found by granting a role in
  // the dev session bar and watching the 403 stay on screen.
  const identity = useIdentityKey()

  // READS ONLY. Nothing in this effect executes anything: it asks whether the buttons should be
  // enabled, and what already happened.
  useEffect(() => {
    let live = true
    void (async () => {
      try {
        const answer = await verifyEligibility(artifactId, inventoryObservationId, environmentId)
        if (!live) return
        setEligible(answer.allowed)
        setEligibility(answer.blockers)
      } catch (error) {
        if (live) setEligibility([])
        if (live) setEligible(false)
        if (live && error instanceof ApiError) setActionError(error.message)
      }
      try {
        const artifact = await getArtifactCode(artifactId)
        if (live) setCode(artifact)
      } catch (error) {
        // A 409 here is the artifact's own subgraph refusal, and its detail says what to fix.
        if (live && error instanceof ApiError) setCodeError(error.message)
      }
      try {
        const status = await getPublicationStatus(environmentId, logicalGroupName)
        if (live) setPublication(status)
      } catch {
        // A publication status this screen could not read is not a reason to hide the rest.
      }
    })()
    return () => { live = false }
  }, [artifactId, environmentId, inventoryObservationId, logicalGroupName, identity])

  const stage: Stage = publication?.blocked
    ? 'published'
    : verification?.verified_output
      ? 'verified'
      : verification
        ? 'verifying'
        : 'generated'

  // THE ONLY CALLER of requestVerification in this application.
  async function onVerify() {
    setActionError(null)
    try {
      const started = await requestVerification({
        sealed_artifact_id: artifactId,
        generation_authorization_revision_id: generationAuthorizationRevisionId,
        check_set_hash: checkSetHash,
        inventory_observation_id: inventoryObservationId,
        environment_id: environmentId,
        attempt,
      })
      setAttempt(attempt + 1)
      setVerification(await getVerificationResult(started.execution_hash))
    } catch (error) {
      if (error instanceof ApiError) setActionError(error.message)
    }
  }

  // THE ONLY CALLER of requestPublication in this application.
  async function onPublish() {
    setActionError(null)
    const output = verification?.verified_output
    if (!output || !verification) return
    try {
      await requestPublication({
        verified_output_revision_id: output.revision_id,
        staging_path: verification.staging_path,
        sealed_artifact_id: artifactId,
        environment_id: environmentId,
        logical_group_name: logicalGroupName,
        publish_mechanism: 'versioned_pointer',
        capability_attestation: capability,
        expected_active_revision_id: null,
        observed_active_revision_id: null,
      })
      setPublication(await getPublicationStatus(environmentId, logicalGroupName))
    } catch (error) {
      if (error instanceof ApiError) setActionError(error.message)
    }
  }

  return (
    <main className="feature-execution">
      {/* GOAL · TARGET · STAGE · OUTPUT, at the top of the workspace (S11's deliverable). What this
          is for and where it has got to, before anything a person can press. */}
      <header className="workspace-header">
        <dl>
          <div>
            <dt>Goal</dt>
            <dd>{goal}</dd>
          </div>
          <div>
            <dt>Target</dt>
            <dd>
              {targetRef ?? (
                <span className="absent">
                  no target — this is an {targetMode} build, which predicts nothing
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt>Stage</dt>
            <dd data-testid="stage">{stage}</dd>
          </div>
          <div>
            <dt>Output</dt>
            <dd>
              {code
                ? `${code.logical_group_name} in ${code.environment_id}`
                : <span className="absent">nothing sealed yet</span>}
            </dd>
          </div>
        </dl>
      </header>

      {/* RESULTS, above intake. What happened is what an operator came here to see. */}
      <section aria-label="Results">
        <h2>Results</h2>
        {actionError && <p className="error">{actionError}</p>}

        {verification ? (
          <div className="verification-result">
            <p>
              Attempt {verification.attempt} staged at <code>{verification.staging_path}</code>
            </p>
            {verification.verified_output ? (
              <p>
                Verified output <code>{verification.verified_output.revision_id}</code>, inputs{' '}
                {verification.verified_output.input_observation_strength}
                {verification.verified_output.reads_enforced
                  ? ' with enforced reads'
                  : ' without enforced reads'}
              </p>
            ) : (
              <p className="absent">
                No verified output yet — a worker is still running this attempt. That is not a
                failure.
              </p>
            )}
          </div>
        ) : (
          <p className="absent">This artifact has not been verified in this session.</p>
        )}

        {publication?.blocked && (
          <p className="warning">
            {publication.detail} (attempt <code>{publication.blocking_attempt_id}</code>,{' '}
            {publication.blocking_outcome})
          </p>
        )}

        {codeError ? (
          <p className="error">{codeError}</p>
        ) : code ? (
          <div className="artifact-code">
            <h3>Generated project — digest <code>{code.project_digest}</code></h3>
            {code.policy_realizations.length > 0 && (
              <div className="policy-provenance">
                <h4>Governed policies applied</h4>
                <ul>
                  {code.policy_realizations.map((link) => (
                    <li key={`${link.revision_id}:${link.occurrence_hash}`}>
                      <code>{link.revision_id}</code> answers{' '}
                      <code>{link.occurrence_hash}</code>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {code.files.map((file) => (
              <details key={file.path}>
                <summary>{file.path}</summary>
                <pre>{file.content}</pre>
              </details>
            ))}
          </div>
        ) : null}
      </section>

      {/* INTAKE, below the results. */}
      <section aria-label="Actions">
        <h2>Actions</h2>
        {eligibility && eligibility.length > 0 && (
          <>
            <p>This artifact cannot be verified yet:</p>
            <BlockerList blockers={eligibility} />
          </>
        )}
        <button type="button" onClick={onVerify} disabled={!eligible}>
          Verify in sandbox
        </button>

        <label>
          Capability attestation
          <input
            value={capability}
            onChange={(event) => setCapability(event.target.value)}
            placeholder="the grant that entitles you to publish"
          />
        </label>
        <button
          type="button"
          onClick={onPublish}
          disabled={!verification?.verified_output || publication?.blocked || !capability}
        >
          Publish to sandbox
        </button>
      </section>
    </main>
  )
}
