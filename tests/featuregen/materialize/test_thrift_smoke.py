"""``scripts/thrift_smoke.py`` — argument and environment handling, and NOTHING REAL IS CONTACTED.

Every test here injects a fake ``connect``. The script's whole purpose is to be the first thing that
touches a new endpoint, so the one thing its own suite must never do is touch one: a test that
opened a socket would be a test that passes or fails on whether someone's laptop happens to have a
metastore, which is the opposite of the guarantee.

The cases that matter are the GUARDS. This script is handed to an operator with a live cluster in
reach, and the failure it could plausibly cause is not a wrong answer — it is being pointed at the
wrong endpoint by an inherited shell environment. So the refusals are pinned harder than the
happy path.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

from featuregen.materialize.metastore_sql import MetastoreEndpoint
from featuregen.materialize.validation import ClusterUnreachable

_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "thrift_smoke.py"


def _module():
    """Load the operator script by path — it lives in ``scripts/``, not in the package."""
    spec = importlib.util.spec_from_file_location("thrift_smoke", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _module()

_ENV = {
    "FEATUREGEN_MATERIALIZE_METASTORE_ENGINE": "hive",
    "FEATUREGEN_MATERIALIZE_METASTORE_HOST": "spark-thrift",
    "FEATUREGEN_MATERIALIZE_METASTORE_PORT": "10000",
    "FEATUREGEN_MATERIALIZE_METASTORE_AUTH": "NONE",
    "FEATUREGEN_MATERIALIZE_METASTORE_PRINCIPAL": "featuregen",
}
_ARGV = ["--schema", "sandbox_feature", "--table", "smoke", "--roles", "featuregen",
         "--confirm-endpoint", "spark-thrift:10000"]


class _Cursor:
    """Answers the three questions the way a healthy Spark Thrift Server would."""

    def __init__(self) -> None:
        self.description: list[list[object]] | None = None
        self._rows: list[tuple[object, ...]] = []

    def execute(self, operation: str) -> None:
        upper = operation.upper()
        if upper.startswith("SHOW PARTITIONS"):
            self.description = [["partition"]]
            self._rows = [("load_dt=2026-08-15",)]
        elif upper.startswith("SHOW TABLES"):
            self.description = [["namespace"], ["tableName"]]
            self._rows = [("sandbox_feature", "smoke")]
        elif upper.startswith("DESCRIBE"):
            self.description = [["col_name"], ["data_type"]]
            self._rows = [("id", "int"), ("load_dt", "string")]
        elif upper.startswith("SHOW GRANT"):
            # SPARK'S REAL ANSWER. It parses SHOW GRANT only to reject it.
            raise RuntimeError(
                "[_LEGACY_ERROR_TEMP_0035] Operation not allowed: SHOW GRANT.(line 1, pos 0)")
        else:                                          # pragma: no cover - defensive
            raise AssertionError(f"unexpected statement {operation!r}")

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.opened_for: list[MetastoreEndpoint] = []

    def cursor(self) -> _Cursor:
        return _Cursor()

    def close(self) -> None:
        return None


def _connect_factory():
    connection = _Connection()

    def connect(endpoint: MetastoreEndpoint):
        connection.opened_for.append(endpoint)
        return connection

    return connect, connection


# ── the guards ───────────────────────────────────────────────────────────────────────────────────


def test_a_MISMATCHED_confirm_endpoint_refuses_WITHOUT_OPENING_ANYTHING(capsys) -> None:
    """The one failure this script could plausibly cause. An operator whose shell already exports a
    real cluster's variables runs it believing it points at the sandbox; `--confirm-endpoint` is
    where that belief is stated and checked. Nothing may be dialled before it matches."""
    connect, connection = _connect_factory()
    argv = ["--schema", "s", "--table", "t", "--roles", "r",
            "--confirm-endpoint", "spark-thrift:10000"]

    code = smoke.main(argv, {**_ENV, "FEATUREGEN_MATERIALIZE_METASTORE_HOST": "prod-metastore"},
                      connect)

    assert code == 2
    assert connection.opened_for == [], "it contacted an endpoint before the guard agreed"
    assert "prod-metastore:10000" in capsys.readouterr().err


@pytest.mark.parametrize("missing", sorted(_ENV))
def test_EVERY_variable_is_REQUIRED_and_none_has_a_default(missing: str, capsys) -> None:
    """A default is how a smoke test written for a sandbox ends up pointed at a production
    metastore, so each of the five must be stated."""
    connect, connection = _connect_factory()
    env = {name: value for name, value in _ENV.items() if name != missing}

    code = smoke.main(_ARGV, env, connect)

    assert code == 2
    assert connection.opened_for == []
    assert missing in capsys.readouterr().err


def test_ALL_missing_variables_are_named_AT_ONCE(capsys) -> None:
    """Naming one gap per run is a script the operator runs five times."""
    code = smoke.main(_ARGV, {}, _connect_factory()[0])

    assert code == 2
    error = capsys.readouterr().err
    for name in _ENV:
        assert name in error


def test_a_NON_NUMERIC_port_is_refused_by_name(capsys) -> None:
    code = smoke.main(_ARGV, {**_ENV, "FEATUREGEN_MATERIALIZE_METASTORE_PORT": "ten-thousand"},
                      _connect_factory()[0])

    assert code == 2
    assert "ten-thousand" in capsys.readouterr().err


def test_the_endpoint_is_built_from_the_LANES_OWN_variable_names() -> None:
    """Pinned so this script cannot drift from what the worker would dial: the names come from
    `queue_lane`, and a literal second spelling here is exactly what this asserts against."""
    endpoint = smoke.endpoint_from_env(_ENV)

    assert (endpoint.host, endpoint.port, endpoint.engine) == ("spark-thrift", 10000, "hive")
    assert (endpoint.auth_mechanism, endpoint.principal) == ("NONE", "featuregen")


def test_a_BLANK_variable_is_treated_as_missing_not_as_a_value() -> None:
    """`MetastoreEndpoint` refuses a blank field, and whitespace must not sneak past as 'stated'."""
    with pytest.raises(smoke.ConfigurationRefused):
        smoke.endpoint_from_env({**_ENV, "FEATUREGEN_MATERIALIZE_METASTORE_HOST": "   "})


# ── the outcomes ─────────────────────────────────────────────────────────────────────────────────


def test_SPARKS_UNANSWERABLE_READ_SCOPE_IS_EXIT_ZERO_because_it_is_an_ANSWER(capsys) -> None:
    """The expected sandbox result, and the reason this script does not simply report pass/fail.
    Spark rejects SHOW GRANT by grammar; that tells the operator something TRUE about the endpoint,
    so it is an outcome rather than a failure. Exiting non-zero here would train an operator to
    treat the platform's most careful refusal as a broken deployment."""
    connect, connection = _connect_factory()

    code = smoke.main(_ARGV, _ENV, connect)

    out = capsys.readouterr().out
    assert code == 0
    assert "READ SCOPE UNANSWERABLE" in out
    assert "1 partitions" in out and "2 columns" in out
    assert connection.opened_for[0].host == "spark-thrift"


def test_an_UNREACHABLE_endpoint_is_exit_THREE(capsys) -> None:
    """The outcome that genuinely means *this endpoint is not usable yet*."""
    def connect(_endpoint):
        raise ClusterUnreachable("Could not connect to spark-thrift:10000")

    code = smoke.main(_ARGV, _ENV, connect)

    assert code == 3
    assert "UNREACHABLE" in capsys.readouterr().out


def test_an_UNRECOGNISED_message_is_exit_THREE_and_says_the_TABLE_is_the_gap(capsys) -> None:
    """An unknown driver message RAISES rather than being folded into a neighbour, and the script
    must report that as unusable — the classifier having no entry is a real gap, not an answer."""
    class _Odd(_Cursor):
        def execute(self, operation: str) -> None:
            raise RuntimeError("something nobody has ever seen before")

    class _OddConnection(_Connection):
        def cursor(self) -> _Odd:
            return _Odd()

    code = smoke.main(_ARGV, _ENV, lambda _endpoint: _OddConnection())

    assert code == 3
    assert "UNRECOGNISED" in capsys.readouterr().out


def test_ROLES_are_required_so_can_read_never_reports_an_UNASKED_no() -> None:
    """`can_read` answers False for an empty role list WITHOUT asking the engine. Printing that as
    a result would be a verdict nothing observed, so argparse refuses the run instead."""
    with pytest.raises(SystemExit):
        smoke.main(["--schema", "s", "--table", "t", "--confirm-endpoint", "spark-thrift:10000"],
                   _ENV, _connect_factory()[0])
