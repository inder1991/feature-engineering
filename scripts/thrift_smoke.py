"""E2 — first contact with a metastore SQL endpoint, THROUGH THE PRODUCTION CODE PATH.

A read-only smoke test the operator runs AFTER applying `deploy/kind/sandbox/41-spark-thrift.yaml`.
It asks L1's three questions about ONE known table and prints the typed outcome of each.

**WHY THIS EXISTS RATHER THAN A beeline SESSION.** A hand-typed `SHOW PARTITIONS` in beeline proves
that a human can reach the endpoint. It does not prove that `SqlMetastoreAdapter` can — which is the
only thing the worker will actually do. The two differ in every way that has bitten this seam
before: the driver and its transport, the exact statement text (back-quoted identifiers this module
validates rather than escapes), reading the `SHOW GRANT` privilege column BY NAME, and above all the
CLASSIFICATION of failures. A beeline session that prints an error tells you something went wrong;
this prints *which of the four typed outcomes it was*, which is the distinction the whole adapter is
built around. So the first real contact with a new endpoint happens through the production objects —
same `MetastoreSession`, same `SqlMetastoreAdapter`, same `FAULT_PATTERNS`.

**IT WRITES NOTHING.** Three metadata reads. It deliberately does NOT exercise
`publish_sql.SqlPublicationSwap`: that one is a `CREATE OR REPLACE VIEW`, a real mutation of the
catalog, and a smoke test is not the thing that should first perform it.

**THE EXPECTED RESULT ON THE KIND SANDBOX IS NOT "ALL THREE GREEN".** That endpoint is a Spark
Thrift Server, and Spark rejects `SHOW GRANT` by grammar (a rule literally named
`unsupportedHiveNativeCommands`), so question 3 reports READ SCOPE UNANSWERABLE. **That is the
designed answer and this script exits 0 for it.** `True` would be the
unconfigured-allowlist-reads-as-everything defect and `False` a denial nobody issued. See the
manifest header and the plan's §6.6b.

**AND IT PRINTS THE DEPLOYMENT'S READ-SCOPE POSTURE BEFORE ASKING ANYTHING** (SUCCESSOR 5, the
user's decision of 2026-08-15). Question 3's outcome says what the ENGINE can do;
`FEATUREGEN_MATERIALIZE_DECLARE_NO_AUTHORIZATION_MODEL` says what the DEPLOYMENT then does about it
— accept it and record a `READ_SCOPE_UNVERIFIED` warning per table, or fail the run closed. Those
are two different facts and an operator reading one without the other cannot predict what a run will
do, so the `declared` line is printed on EVERY invocation, in both states, and loudly when it is
set. Nothing here reads it to change behaviour: this script asks metadata questions and never runs
L1.

USAGE (operator; every value explicit, nothing defaulted):

    FEATUREGEN_MATERIALIZE_METASTORE_ENGINE=hive \\
    FEATUREGEN_MATERIALIZE_METASTORE_HOST=spark-thrift \\
    FEATUREGEN_MATERIALIZE_METASTORE_PORT=10000 \\
    FEATUREGEN_MATERIALIZE_METASTORE_AUTH=NONE \\
    FEATUREGEN_MATERIALIZE_METASTORE_PRINCIPAL=featuregen \\
    python scripts/thrift_smoke.py --schema sandbox_feature --table smoke \\
        --roles featuregen --confirm-endpoint spark-thrift:10000

**THE GUARDS, and what each one is actually for.** The endpoint comes from the SAME five environment
variables the lane reads — imported by name, never re-spelled here, so this cannot drift from what
the worker would dial. **None has a default**, because a default is how a smoke test written for a
sandbox ends up pointed at a production metastore. And `--confirm-endpoint` must equal the
`host:port` the environment resolved to: the operator states where they BELIEVE they are pointing,
and a mismatch refuses before a socket is opened. That is the one failure this script could
otherwise cause — an inherited shell environment silently aiming it somewhere real.

**IT NEEDS A DRIVER THAT IS NOT A DEPENDENCY OF THIS PROJECT.** `METASTORE_DRIVERS` names PyHive,
and the control plane carries no engine client on purpose, so `pip install "PyHive[hive]" thrift`
into the pod is a prerequisite. The adapter's own refusal names the package if it is missing.

EXIT CODES: `0` every question reached the engine and produced a typed outcome (READ SCOPE
UNANSWERABLE included — it is an answer ABOUT the endpoint). `2` the configuration was refused
before any connection. `3` the endpoint could not be reached, or answered something the classifier
does not recognise — the two outcomes that mean *this endpoint is not usable yet*.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence

from featuregen.materialize.metastore_sql import (
    MetastoreAnswerRefused,
    MetastoreEndpoint,
    MetastoreFaultError,
    MetastoreReadScopeUnanswerable,
    MetastoreSession,
    SqlConnection,
    SqlMetastoreAdapter,
)

# THE PRIVATE NAMES ARE IMPORTED DELIBERATELY. Spelling these five strings literally here would be a
# SECOND source of truth for the variables a deployment sets, and the entire value of this script is
# that it dials exactly what the worker would dial. If `queue_lane` renames one, this must break.
from featuregen.materialize.queue_lane import (
    _DECLARE_NO_AUTHORIZATION_MODEL_ENV,
    _METASTORE_AUTH_ENV,
    _METASTORE_ENGINE_ENV,
    _METASTORE_HOST_ENV,
    _METASTORE_PORT_ENV,
    _METASTORE_PRINCIPAL_ENV,
)
from featuregen.materialize.validation import ClusterUnreachable, MetastoreTableUnknown

_CONFIG_REFUSED = 2
_ENDPOINT_UNUSABLE = 3


class ConfigurationRefused(Exception):
    """The environment or the arguments do not describe one endpoint, so nothing is contacted."""


def endpoint_from_env(env: Mapping[str, str]) -> MetastoreEndpoint:
    """The five variables → one :class:`MetastoreEndpoint`, or a refusal naming what is missing.

    EVERY missing name is reported at once. A smoke test that names one gap per run is a smoke test
    the operator runs five times, and `MetastoreEndpoint` itself refuses a blank field for the same
    reason — "an endpoint nobody fully stated is not a posture, it is half a configuration".
    """
    names = (_METASTORE_ENGINE_ENV, _METASTORE_HOST_ENV, _METASTORE_PORT_ENV,
             _METASTORE_AUTH_ENV, _METASTORE_PRINCIPAL_ENV)
    missing = [name for name in names if not (env.get(name) or "").strip()]
    if missing:
        raise ConfigurationRefused(
            "the metastore endpoint is not fully stated — set " + ", ".join(missing) + ". None of "
            "these has a default: a default is how a smoke test written for a sandbox ends up "
            "pointed at a production metastore")

    raw_port = (env[_METASTORE_PORT_ENV] or "").strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigurationRefused(
            f"{_METASTORE_PORT_ENV} is {raw_port!r}, which is not a whole number") from None

    try:
        return MetastoreEndpoint(
            engine=(env[_METASTORE_ENGINE_ENV] or "").strip(),
            host=(env[_METASTORE_HOST_ENV] or "").strip(),
            port=port,
            auth_mechanism=(env[_METASTORE_AUTH_ENV] or "").strip(),
            principal=(env[_METASTORE_PRINCIPAL_ENV] or "").strip())
    except ValueError as error:                        # the endpoint's own validation, re-typed
        raise ConfigurationRefused(str(error)) from error


def _declaration_line(env: Mapping[str, str]) -> str:
    """The deployment's READ-SCOPE POSTURE, printed at first contact — LOUDLY when it is declared.

    This script is the first thing that touches a new endpoint, so it is the right place to show
    the one setting that decides whether an unanswerable read scope stops a run. Printed on every
    invocation, in both states: a line that appeared only when the declaration was set would leave
    an operator unable to tell "not declared" from "this script does not know about it".

    Read from the environment the caller passed, exactly as ``queue_lane`` reads it, through the
    lane's OWN variable name — not a literal spelled here, which would be a second source of truth
    for the setting whose whole risk is being set somewhere nobody looked.
    """
    stated = (env.get(_DECLARE_NO_AUTHORIZATION_MODEL_ENV) or "").strip()
    if stated.lower() in {"1", "true", "yes", "on"}:
        return (f"*** {_DECLARE_NO_AUTHORIZATION_MODEL_ENV}={stated} — THIS DEPLOYMENT DECLARES IT "
                f"HAS NO AUTHORIZATION MODEL. A run here proceeds WITHOUT anyone verifying that "
                f"the authorized roles may read its inputs; every accepted table is recorded as a "
                f"READ_SCOPE_UNVERIFIED warning on the run's own L1 report. Correct for a sandbox "
                f"whose engine cannot answer SHOW GRANT. NEVER set it on a real deployment. ***")
    return (f"{_DECLARE_NO_AUTHORIZATION_MODEL_ENV} is not set (the strict posture): an "
            f"unanswerable read scope FAILS a run closed. This is the correct state for every "
            f"deployment whose engine has an authorization model")


def _report(label: str, run: Callable[[], str]) -> bool:
    """Run one question, print its TYPED outcome, and say whether the endpoint answered.

    Returns ``False`` only for the two outcomes that mean the endpoint is not usable: it could not
    be reached, or it said something `FAULT_PATTERNS` does not recognise. Every other outcome —
    including *the table does not exist* and *read scope is unanswerable* — is an ANSWER, and a
    smoke test that called those failures would be reporting a verdict about a feature rather than
    about an endpoint.
    """
    try:
        print(f"  {label:<22} OK              {run()}")
        return True
    except MetastoreTableUnknown as error:
        print(f"  {label:<22} TABLE UNKNOWN   {error}")
        return True
    except MetastoreReadScopeUnanswerable as error:
        print(f"  {label:<22} READ SCOPE UNANSWERABLE")
        print(f"  {'':<22} {error}")
        print(f"  {'':<22} ^ EXPECTED against a Spark Thrift Server — Spark rejects SHOW GRANT by")
        print(f"  {'':<22}   grammar. It is the designed answer, not a fault.")
        print(f"  {'':<22}   Whether a RUN then proceeds depends on the declaration printed above:")
        print(f"  {'':<22}   declared, L1 accepts this and records READ_SCOPE_UNVERIFIED per table;")
        print(f"  {'':<22}   undeclared, L1 fails the run closed.")
        return True
    except MetastoreAnswerRefused as error:
        print(f"  {label:<22} REFUSED         {error}")
        return True
    except ClusterUnreachable as error:
        print(f"  {label:<22} UNREACHABLE     {error}")
        return False
    except MetastoreFaultError as error:
        print(f"  {label:<22} UNRECOGNISED    {error}")
        print(f"  {'':<22} ^ the classifier has no entry for this message. That is a gap in")
        print(f"  {'':<22}   FAULT_PATTERNS, and it RAISES rather than being guessed at.")
        return False


def main(argv: Sequence[str] | None = None,
         env: Mapping[str, str] | None = None,
         connect: Callable[[MetastoreEndpoint], SqlConnection] | None = None) -> int:
    """``env`` and ``connect`` are seams so the suite proves this file without a socket."""
    parser = argparse.ArgumentParser(
        description="Ask L1's three questions of a metastore SQL endpoint, through the "
                    "production adapter. Read-only.")
    parser.add_argument("--schema", required=True, help="the schema of a table that EXISTS")
    parser.add_argument("--table", required=True, help="the table to ask about")
    parser.add_argument(
        "--roles", required=True, nargs="+",
        help="role names for the read-scope question. REQUIRED and non-empty: `can_read` answers "
             "False for an empty role list WITHOUT asking the engine anything, and printing that "
             "as a result would be a verdict nothing observed.")
    parser.add_argument(
        "--confirm-endpoint", required=True, metavar="HOST:PORT",
        help="the host:port you BELIEVE the environment names. Refused unless it matches, before "
             "any connection is opened.")
    args = parser.parse_args(argv)

    environ = os.environ if env is None else env
    try:
        endpoint = endpoint_from_env(environ)
    except ConfigurationRefused as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return _CONFIG_REFUSED

    resolved = f"{endpoint.host}:{endpoint.port}"
    if args.confirm_endpoint.strip() != resolved:
        print(f"REFUSED: --confirm-endpoint says {args.confirm_endpoint.strip()!r} and the "
              f"environment resolves to {resolved!r}. Nothing was contacted. This is the guard "
              f"against an inherited shell environment aiming a sandbox smoke test at a real "
              f"cluster.", file=sys.stderr)
        return _CONFIG_REFUSED

    print(f"endpoint  {resolved}  engine={endpoint.engine}  auth={endpoint.auth_mechanism}  "
          f"principal={endpoint.principal}")
    print(f"table     {args.schema}.{args.table}")
    print(f"roles     {' '.join(args.roles)}")
    print(f"declared  {_declaration_line(environ)}")
    print("asking L1's three questions (read-only; nothing is written)")

    session = (MetastoreSession(endpoint=endpoint) if connect is None
               else MetastoreSession(endpoint=endpoint, connect=connect))
    adapter = SqlMetastoreAdapter(session=session)
    try:
        answered = [
            _report("1 list_partitions", lambda: _partitions(adapter, args.schema, args.table)),
            _report("2 describe_table", lambda: _describe(adapter, args.schema, args.table)),
            _report("3 can_read", lambda: _can_read(adapter, args.schema, args.table, args.roles)),
        ]
    finally:
        session.close()

    if not all(answered):
        print("\nthe endpoint is NOT usable yet — see the outcomes above", file=sys.stderr)
        return _ENDPOINT_UNUSABLE
    print("\nevery question produced a typed outcome; the endpoint is reachable and classified")
    return 0


def _partitions(adapter: SqlMetastoreAdapter, schema: str, table: str) -> str:
    listed = adapter.list_partitions(schema=schema, table=table)
    if not listed:
        # The ONE empty the adapter may produce, and it is never silence — it means the engine
        # positively said this table is not partitioned.
        return "0 partitions (the engine ANSWERED: not a partitioned table)"
    return f"{len(listed)} partitions, first={listed[0]}"


def _describe(adapter: SqlMetastoreAdapter, schema: str, table: str) -> str:
    described = adapter.describe_table(schema=schema, table=table)
    if described is None:
        return "None — the engine says this table does not exist"
    return f"{len(described)} columns, first={described[0]}"


def _can_read(adapter: SqlMetastoreAdapter, schema: str, table: str, roles: Sequence[str]) -> str:
    return "YES" if adapter.can_read(schema=schema, table=table, roles=roles) else "NO"


if __name__ == "__main__":                             # pragma: no cover - operator entry point
    raise SystemExit(main())
