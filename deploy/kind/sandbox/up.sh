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

echo "==> applying the Thrift/SQL endpoint"
# AFTER the schema init and the Derby check, and the order is load-bearing: this server opens its
# metastore connection while it starts, so applying it against an uninitialised `metastore` database
# gives a pod that comes up and then fails every statement. It is a SECOND deployment on the SAME
# image and the SAME spark-defaults ConfigMap — one engine, one catalog. See the manifest header for
# why it is `spark-submit` and not `sbin/start-thriftserver.sh` (which the pyspark distribution does
# not ship) and for what its authorization posture is and is not.
kubectl apply -f "$HERE/41-spark-thrift.yaml"
kubectl -n "$NS" rollout restart deploy/spark-thrift
# Longer than deploy/spark's 180s: this one is not a `sleep infinity` shell — readiness means a JVM
# started, the Hive client initialised, the metastore answered and the port bound.
kubectl -n "$NS" rollout status deploy/spark-thrift --timeout=300s

cat <<'EOF'

Ready.
  kubectl -n featuregen exec -it deploy/spark -- python
  Tables land in `sandbox_feature`; the catalog is shared via Postgres, files on the warehouse PVC.

The SQL endpoint is `spark-thrift:10000` inside the cluster (ClusterIP — never published to the
host; it authenticates nobody).

  VERIFY IT THROUGH THE PRODUCTION CODE PATH, not beeline. Two things must be delivered first,
  because the backend image carries NEITHER (`Dockerfile.backend` copies only `src/`, and PyHive is
  not a dependency of this project — the control plane carries no engine client on purpose):

    kubectl -n featuregen exec deploy/backend -- pip install "PyHive[hive]" thrift
    kubectl -n featuregen cp scripts/thrift_smoke.py featuregen/$(kubectl -n featuregen get pod \
      -l app=backend -o jsonpath='{.items[0].metadata.name}'):/tmp/thrift_smoke.py

    kubectl -n featuregen exec deploy/backend -- env \
      FEATUREGEN_MATERIALIZE_METASTORE_ENGINE=hive \
      FEATUREGEN_MATERIALIZE_METASTORE_HOST=spark-thrift \
      FEATUREGEN_MATERIALIZE_METASTORE_PORT=10000 \
      FEATUREGEN_MATERIALIZE_METASTORE_AUTH=NONE \
      FEATUREGEN_MATERIALIZE_METASTORE_PRINCIPAL=featuregen \
      FEATUREGEN_MATERIALIZE_DECLARE_NO_AUTHORIZATION_MODEL=1 \
      python /tmp/thrift_smoke.py --schema sandbox_feature --table smoke \
        --roles featuregen --confirm-endpoint spark-thrift:10000

  That script drives the SAME `materialize/metastore_sql.py` adapter the worker uses and prints one
  typed outcome per question, so the first real contact with this endpoint happens through the exact
  production code path. EXPECT L1's third question to report READ SCOPE UNANSWERABLE: this endpoint
  is Spark, and Spark rejects `SHOW GRANT` by grammar. That is the designed answer, not a fault —
  see the manifest header and the plan's §6.6b.

  THE LAST VARIABLE ABOVE IS A DECLARATION, NOT A SETTING, and the script prints it back at you
  before it asks anything. It says: this deployment has no authorization model, so accept an
  unverified read scope. With it, a RUN gets past L1 and records a READ_SCOPE_UNVERIFIED warning per
  table on its own validation report (visible in plain language at GET /materialization-runs/{id}
  and on the run screen); without it, a run against this endpoint still fails closed at L1. Drop it
  from the line above to see the strict posture. **Never set it on a real deployment** — it belongs
  to a sandbox whose engine cannot answer, and to nothing else. The user's decision of 2026-08-15;
  see plan §6.6d and deploy/kind/k8s/20-backend.yaml for the full block.
EOF
