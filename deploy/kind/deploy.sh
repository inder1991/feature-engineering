#!/usr/bin/env bash
# Canonical build + deploy for the FeatureGen demo on a local kind cluster.
# ONE script every session uses, so the running image never drifts from the checkout.
#
#   ./deploy/kind/deploy.sh            # build + deploy everything (backend, frontend, postgres)
#   ./deploy/kind/deploy.sh backend    # rebuild + redeploy ONLY the backend (fast iteration)
#   ./deploy/kind/deploy.sh frontend   # rebuild + redeploy ONLY the frontend
#
# The LLM key (never committed) is sourced, in order, from:
#   1. $ANTHROPIC_API_KEY in your environment, or
#   2. deploy/kind/.llm-key   (a gitignored one-line file you create locally), or
#   3. the existing in-cluster `featuregen-llm` secret (left as-is if already set).
#
# WHY force a rollout: `kubectl apply` does NOT restart pods when the image tag is unchanged
# (`:local`), so a freshly-built image would otherwise never be picked up — the classic
# "I rebuilt but it's still running old code" drift that stales the running catalog. We
# `kind load` the new image, then `rollout restart` so pods actually run it, then verify
# the code is really being served (not just "pod is up").
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-featuregen}"
NS="${KIND_NAMESPACE:-featuregen}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
TARGET="${1:-all}"   # all | backend | frontend

log() { printf '\n==> %s\n' "$*"; }
want() { [ "$TARGET" = "all" ] || [ "$TARGET" = "$1" ]; }

# ── 1. cluster + namespace ───────────────────────────────────────────────────────────────────────
log "kind cluster '$CLUSTER'"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER"
kubectl apply -f deploy/kind/k8s/00-namespace.yaml

# ── 2. build + load the requested images (context = repo root, so COPY src picks up THIS checkout) ─
build_load() {  # $1 = short name (backend|frontend)
  local name="$1"
  log "build featuregen-${name}:local (from $ROOT)"
  docker build -t "featuregen-${name}:local" -f "deploy/kind/Dockerfile.${name}" .
  log "load featuregen-${name}:local into kind"
  kind load docker-image "featuregen-${name}:local" --name "$CLUSTER"
}
want backend  && build_load backend
want frontend && build_load frontend

# ── 3. manifests (idempotent) ─────────────────────────────────────────────────────────────────────
log "apply manifests"
kubectl apply -f deploy/kind/k8s/

# ── 4. LLM secret (never committed; only (re)created when a key is available) ───────────────────────
KEY="${ANTHROPIC_API_KEY:-}"
[ -z "$KEY" ] && [ -f deploy/kind/.llm-key ] && KEY="$(tr -d '\r\n' < deploy/kind/.llm-key)"
if [ -n "$KEY" ]; then
  log "set featuregen-llm secret from provided key"
  kubectl -n "$NS" create secret generic featuregen-llm \
    --from-literal=ANTHROPIC_API_KEY="$KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
elif kubectl -n "$NS" get secret featuregen-llm >/dev/null 2>&1; then
  log "featuregen-llm secret already present — leaving as-is (no key provided this run)"
else
  log "WARNING: no ANTHROPIC_API_KEY, no deploy/kind/.llm-key, no existing secret — LLM stages will fail closed until you provide a key and re-run."
fi

# ── 5. force pods onto the freshly-loaded image, then wait ──────────────────────────────────────────
log "restart + wait for rollouts (the step that defeats stale-image drift)"
kubectl -n "$NS" rollout status deploy/postgres --timeout=180s
restart_wait() {  # $1 = deploy name
  kubectl -n "$NS" rollout restart "deploy/$1"
  kubectl -n "$NS" rollout status "deploy/$1" --timeout=300s
}
want backend  && restart_wait backend
want frontend && restart_wait frontend

# ── 6. verify the running image actually serves the code (not just "pod is up") ─────────────────────
log "verify"
kubectl -n "$NS" get pods -o wide
( kubectl -n "$NS" port-forward svc/backend 8000:8000 >/dev/null 2>&1 & ) ; PF=$!
sleep 3
HEALTH="$(curl -s -m 5 localhost:8000/health || true)"
ROUTES="$(curl -s -m 5 localhost:8000/openapi.json | grep -c '/catalog/assets' || echo 0)"
kill "$PF" 2>/dev/null || true
echo "backend /health: ${HEALTH:-<none>}"
if [ "${ROUTES:-0}" -gt 0 ]; then
  echo "asset-detail route registered: yes (image is serving current code)"
else
  echo "asset-detail route registered: NO — the running image is stale or a router failed to import; investigate before using the demo."
  exit 1
fi

cat <<EOF

Ready. Access:
  kubectl -n $NS port-forward svc/frontend 8080:80    # http://localhost:8080
  kubectl -n $NS port-forward svc/backend  8000:8000  # http://localhost:8000/health
Tear down:  kind delete cluster --name $CLUSTER
EOF
