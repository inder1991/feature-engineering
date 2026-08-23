import type { Route } from '../nav'

type Navigate = (r: Route, params?: Record<string, string> | URLSearchParams) => void

// The feature-run list, grouped by hypothesis (GET /feature-runs). PLACEHOLDER: Task 11 landed the
// route and this stub so '#/runs' resolves to a real screen rather than a blank main; Task 12
// replaces the body with the grouped list. The signature is already the final one — `navigate` is
// how a run row opens its detail — so Task 12 changes the body only, never the call site in App.
export function RunsScreen({ navigate }: { navigate: Navigate }) {
  // `void` only because noUnusedParameters is on and the stub has nothing to navigate FROM yet.
  // Task 12 deletes this line the moment a run row becomes clickable.
  void navigate
  return <p className="muted">Feature runs are not listed yet.</p>
}
