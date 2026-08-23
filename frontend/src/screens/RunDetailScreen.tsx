// One feature run opened to its record (GET /feature-runs/{id}). PLACEHOLDER: Task 11 landed the
// '#/runs/<id>' path-param route and this stub; Task 13 replaces the body with the identity,
// milestones, authoring rows and stage rail. The signature is already the final one — the opaque
// run id, decoded from the path — so Task 13 changes the body only, never App's call site.
export function RunDetailScreen({ runId }: { runId: string }) {
  return <p className="muted">Run {runId} is not detailed yet.</p>
}
