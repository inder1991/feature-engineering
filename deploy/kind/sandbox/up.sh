#!/usr/bin/env bash
# Local Spark 3.5 + Hive-metastore sandbox for the materialization work.
#
# OPT-IN, not part of `deploy.sh`. It lives outside `k8s/` on purpose: `deploy.sh` applies that
# whole directory, and a cluster without this image built would land an ImagePullBackOff.
#
# Run:  bash deploy/kind/sandbox/up.sh
# Use:  kubectl -n featuregen exec -it deploy/spark -- python
#
# WHAT THIS BUYS. The generated Kedro project can execute against a REAL metastore: table
# creation, partitioning, schema resolution, and the publication code path. WHAT IT DOES NOT:
# HDFS atomicity. The warehouse here is an ordinary filesystem on a local-path volume, and HDFS
# rename/visibility guarantees genuinely differ, so a capability attestation earned here is
# environment-scoped and correctly refuses to authorize the real cluster.
set -euo pipefail

NS=featuregen
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Matches the Hive client pyspark 3.5 bundles (2.3.9). A different Spark line needs a different script.
HIVE_SCHEMA_URL="https://raw.githubusercontent.com/apache/hive/rel/release-2.3.9/metastore/scripts/upgrade/postgres/hive-schema-2.3.0.postgres.sql"

echo "==> building the sandbox image (Spark 3.5 on Python 3.11)"
# 3.5 deliberately: Spark 3 defaults spark.sql.ansi.enabled FALSE, Spark 4 TRUE, and that decides
# whether a decimal overflow RAISES or returns NULL — the rendered OVERFLOW_VIOLATION gate is
# built for the NULL behaviour. Python 3.11 deliberately: the stock apache/spark:3.5.3 image ships
# Python 3.8, on which `pip install kedro==1.5.0` resolves nothing above 0.19.8, so it cannot run
# the project this repo generates.
docker build -f "$HERE/Dockerfile.spark" -t featuregen-spark:local "$HERE"
kind load docker-image featuregen-spark:local --name featuregen

PG=$(kubectl -n "$NS" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}')

echo "==> creating the metastore database if absent"
if ! kubectl -n "$NS" exec "$PG" -- psql -U postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='metastore'" | grep -q 1; then
  kubectl -n "$NS" exec "$PG" -- psql -U postgres -c "CREATE DATABASE metastore;"
fi

echo "==> initialising the Hive schema from Hive's own DDL"
# NEVER let application sessions build this. DataNucleus `autoCreateAll` self-deadlocks partway —
# consistently at 28 of 46 tables, one connection left "idle in transaction" while an
# ALTER TABLE ... ADD CONSTRAINT waits on it — even from a SINGLE session. Applying the official
# script takes seconds, so the sandbox config sets autoCreateAll=false and this is the bootstrap.
TABLES=$(kubectl -n "$NS" exec "$PG" -- psql -U postgres -d metastore -tAc \
          "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
if [ "$TABLES" -lt 40 ]; then
  TMP=$(mktemp)
  curl -fsSL -o "$TMP" "$HIVE_SCHEMA_URL"
  kubectl -n "$NS" cp "$TMP" "$NS/$PG:/tmp/hive-schema.sql"
  kubectl -n "$NS" exec "$PG" -- psql -U postgres -d metastore -q -f /tmp/hive-schema.sql
  rm -f "$TMP"
  echo "    schema applied"
else
  echo "    already initialised ($TABLES tables)"
fi

echo "==> applying the sandbox manifest"
kubectl apply -f "$HERE/40-spark.yaml"
# `kubectl apply` does not restart pods when only a ConfigMap changed, and a subPath mount NEVER
# picks up a ConfigMap update — so a config edit is invisible without this.
kubectl -n "$NS" rollout restart deploy/spark
kubectl -n "$NS" rollout status deploy/spark --timeout=180s

echo "==> verifying the catalog is Postgres-backed, not Derby"
# The assertion that matters. A write succeeding proves nothing: Spark falls back to an embedded
# Derby metastore silently, and a second SEQUENTIAL session still sees the tables. Only the
# presence of the table in Postgres distinguishes them.
kubectl -n "$NS" exec deploy/spark -- python - <<'PY' >/dev/null 2>&1
from pyspark.sql import SparkSession
s = SparkSession.builder.appName("sandbox-verify").master("local[1]").enableHiveSupport().getOrCreate()
s.sparkContext.setLogLevel("ERROR")
s.sql("CREATE DATABASE IF NOT EXISTS sandbox_feature")
s.sql("CREATE TABLE IF NOT EXISTS sandbox_feature.smoke (id INT) USING parquet")
s.stop()
PY
FOUND=$(kubectl -n "$NS" exec "$PG" -- psql -U postgres -d metastore -tAc \
         "SELECT count(*) FROM \"TBLS\" WHERE \"TBL_NAME\"='smoke'")
if [ "$FOUND" -ge 1 ]; then
  echo "    OK — catalog is in Postgres and shared"
else
  echo "    FAILED — the table is not in the Postgres metastore (Derby fallback?)" >&2
  exit 1
fi

cat <<'EOF'

Ready.
  kubectl -n featuregen exec -it deploy/spark -- python
  Tables land in `sandbox_feature`; the catalog is shared via Postgres, files on the warehouse PVC.
EOF
