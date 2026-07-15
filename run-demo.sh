#!/usr/bin/env bash
# Launch the FeatureGen API with every governed pass turned ON (full e2e demo).
# The app does not auto-load .env, so this sources .env.demo explicitly.
#
#   ./run-demo.sh          # start the API on :8000 with all governed flags on
#
# In a second terminal, run the frontend:  make frontend-dev   (Vite on :5173)
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${DEMO_ENV_FILE:-.env.demo}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: $ENV_FILE not found (copy .env.demo and fill in the secrets)" >&2
  exit 1
fi

# Export every var defined in the demo env file.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Fail fast on the two things only you can supply.
if [[ -z "${FEATUREGEN_DSN:-}" ]]; then
  echo "error: FEATUREGEN_DSN is unset — point it at your local Postgres in $ENV_FILE" >&2
  exit 1
fi
if [[ "${ANTHROPIC_API_KEY:-}" == "sk-ant-REPLACE_ME" || -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "warning: ANTHROPIC_API_KEY is a placeholder — enrichment + Pass B will be SKIPPED." >&2
  echo "         Pass C joins still run; set a real key in $ENV_FILE for the full pipeline." >&2
fi

echo "== FeatureGen full-pipeline demo =="
echo "  governed joins : ${OVERLAY_GOVERNED_JOINS:-0}"
echo "  Pass C (joins) : ${OVERLAY_PASS_C:-0}"
echo "  Pass B (synth) : ${OVERLAY_TABLE_SYNTH:-0}"
echo "  LLM provider   : ${FEATUREGEN_LLM_PROVIDER:-<none>}"
echo "  auto-migrate   : ${FEATUREGEN_AUTO_MIGRATE:-0}"
echo "  API            : http://localhost:8000  (docs at /docs)"
echo "  Frontend       : run 'make frontend-dev' in another terminal -> http://localhost:5173"
echo

exec uv run uvicorn --factory featuregen.api.app:create_app_from_env --reload --port 8000
