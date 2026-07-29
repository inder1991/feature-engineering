#!/usr/bin/env bash
# Local Apache Hive 4 for validating the Release 3 analysis compiler.
#
# WHY THIS EXISTS: every test of the analysis compiler ran against PostgreSQL, and the two engines
# disagree about things PostgreSQL cannot reveal — identifier quoting (a double-quoted token is an
# identifier in PostgreSQL and a STRING LITERAL in HiveQL), namespace arity, and whether a partition
# is actually pruned. Those defects do not raise; they return a well-formed wrong answer.
#
# WHAT THIS IS NOT: a stand-in for the bank cluster. It runs synthetic pilot rows, so it proves the
# ENGINE and the DIALECT, never that the catalog describes the bank's tables correctly.
#
# Deliberately a standalone container, NOT part of deploy/kind — the demo cluster carries customer
# catalog state and must not gain a dependency on a throwaway analytics engine.
set -euo pipefail

NAME="${HIVE_CONTAINER:-featuregen-hive}"
IMAGE="${HIVE_IMAGE:-apache/hive:4.1.0}"
PORT="${HIVE_PORT:-10000}"

if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ]; then
  echo "$NAME already running on port $PORT"
  exit 0
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --platform linux/amd64 \
  -p "$PORT:10000" -p 10002:10002 \
  -e SERVICE_NAME=hiveserver2 \
  "$IMAGE" >/dev/null

echo "starting $NAME (HiveServer2 on $PORT, web UI on 10002)"
for _ in $(seq 1 90); do
  if docker exec "$NAME" beeline -u "jdbc:hive2://localhost:10000" -e "SELECT 1" >/dev/null 2>&1; then
    echo "ready"
    exit 0
  fi
  sleep 5
done

echo "HiveServer2 did not become ready; logs:" >&2
docker logs --tail 40 "$NAME" >&2
exit 1
