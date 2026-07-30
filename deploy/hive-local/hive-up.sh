#!/usr/bin/env bash
# Local Apache Hive for validating the Release 3 analysis compiler.
#
# WHY THIS EXISTS: every test of the analysis compiler ran against PostgreSQL, and the two engines
# disagree about things PostgreSQL cannot reveal — identifier quoting (a double-quoted token is an
# identifier in PostgreSQL and a STRING LITERAL in HiveQL), namespace arity (HiveQL refuses a
# three-part table name), and whether a partition is actually pruned. Those defects do not raise;
# they return a well-formed wrong answer.
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

# A RUNNING container is not a ready engine, and this is the whole reliability story of the script.
# HiveServer2 takes a minute or two to boot; it can WEDGE while the container still reports `Up`
# (observed at 22 hours uptime, beeline hanging past two minutes); and the embedded Derby metastore
# does not reliably survive a `docker restart`, which is how it got wedged. So: always probe, and
# treat a running-but-unresponsive container as something to REPLACE rather than wait on. The engine
# is disposable — the pilot fixture reloads its tables on every run.
probe() {
  # `timeout` bounds a hung server. Without it a wedged HiveServer2 blocks forever instead of
  # failing this check and letting the caller act.
  timeout 20 docker exec "$NAME" beeline -u "jdbc:hive2://localhost:10000" \
    -e "SELECT 1" >/dev/null 2>&1
}

create() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  # linux/arm64: this image publishes a native arm64 manifest, and forcing amd64 on Apple Silicon
  # runs the JVM under emulation.
  docker run -d --name "$NAME" --platform linux/arm64 \
    -p "$PORT:10000" -p 10002:10002 \
    -e SERVICE_NAME=hiveserver2 \
    "$IMAGE" >/dev/null
  echo "starting $NAME (HiveServer2 on $PORT, web UI on 10002)"
}

wait_ready() {                       # wait_ready <attempts>
  for _ in $(seq 1 "$1"); do
    if probe; then echo "ready"; return 0; fi
    sleep 5
  done
  return 1
}

if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ]; then
  echo "$NAME is running on port $PORT; probing HiveServer2"
  # Only a SHORT grace: every failed probe costs the full 20s timeout against a hung server,
  # so a generous loop here spends minutes proving what two attempts already showed.
  if wait_ready 3; then exit 0; fi
  echo "$NAME is up but not answering — replacing it" >&2
fi

create
if wait_ready 90; then exit 0; fi

echo "HiveServer2 did not become ready; logs:" >&2
docker logs --tail 40 "$NAME" >&2
exit 1
