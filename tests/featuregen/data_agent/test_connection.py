"""Governed source connections — Release 1 step 5's prerequisite.

The first thing that exists once real access is possible, because it is the thing that decides what
a real connection may touch. Two properties carry almost all the weight:

* **A connection holds no secret.** Only a reference. A field that *could* hold a credential
  eventually holds one, and then it is in a log, a JSON column, an error message or an LLM prompt.
* **A binding cannot read outside its connection's allowlist.** Steps 1-4 validate the plan against
  the *binding*; this validates the binding against the *connection*. Without it, a correct-looking
  binding could address a schema the connection was never approved for.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.connection import (
    ConnectionError_,
    DataSourceConnectionV1,
    authorize_binding,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1


def _conn(**over) -> DataSourceConnectionV1:
    kw = dict(connection_id="hive-pilot", environment_id="uat", kind="hive",
              host="hiveserver2.internal", port=10000, auth_mechanism="kerberos",
              secret_ref="vault://featuregen/hive-pilot", execution_principal="svc_featuregen_ro",
              allowed_schemas=frozenset({"dpl_eib"}), active=True)
    kw.update(over)
    return DataSourceConnectionV1(**kw)


def _binding(schema="dpl_eib", **over) -> PhysicalDatasetBindingV1:
    identity = PhysicalObjectIdentityV1(catalog_source="ftr", database="banking", schema=schema,
                                        table="tran_repos", object_kind="table")
    kw = dict(binding_id="b-1", catalog_logical_ref="ftr::dpl_eib.tran_repos",
              connection_id="hive-pilot", identity=identity,
              partition_columns=("tran_date",), business_time_column="tran_date")
    kw.update(over)
    return PhysicalDatasetBindingV1(**kw)


# ── no secret ever lives on the connection ───────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_ref", [
    "password=hunter2",
    "jdbc:hive2://user:pw@host:10000/db",
    "-----BEGIN PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.abc.def",
])
def test_a_secret_shaped_reference_is_refused(bad_ref):
    """A reference names WHERE a secret lives. Anything that looks like the secret itself is
    refused, because the field would otherwise become the place someone pastes one."""
    with pytest.raises(ConnectionError_, match="secret"):
        _conn(secret_ref=bad_ref)


def test_a_reference_must_actually_be_a_reference():
    with pytest.raises(ConnectionError_):
        _conn(secret_ref="")


def test_the_connection_never_serializes_a_credential():
    text = repr(_conn())
    for smell in ("password", "keytab", "hunter2", "jdbc:", "begin private key"):
        assert smell not in text.lower()


# ── the allowlist is the boundary a binding cannot cross ─────────────────────────────────────────

def test_a_binding_inside_the_allowlist_is_authorized():
    assert authorize_binding(_conn(), _binding()).connection_id == "hive-pilot"


def test_a_binding_outside_the_allowlist_is_refused():
    """The property steps 1-4 could not check: a binding validates its own shape, but only the
    connection knows which schemas were approved for it."""
    with pytest.raises(ConnectionError_, match="allowlist"):
        authorize_binding(_conn(), _binding(schema="hr_private"))


def test_an_empty_allowlist_authorizes_nothing():
    """Fail closed. An unconfigured allowlist must not read as 'everything' — that is the
    caller-forged-allowlist hole the shipped profiler_command already refuses."""
    with pytest.raises(ConnectionError_, match="allowlist"):
        authorize_binding(_conn(allowed_schemas=frozenset()), _binding())


def test_a_binding_for_another_connection_is_refused():
    with pytest.raises(ConnectionError_, match="connection"):
        authorize_binding(_conn(connection_id="other"), _binding())


# ── lifecycle ────────────────────────────────────────────────────────────────────────────────────

def test_a_disabled_connection_cannot_be_used():
    with pytest.raises(ConnectionError_, match="not active"):
        authorize_binding(_conn(active=False), _binding())


def test_an_unsupported_kind_is_refused_rather_than_attempted():
    """Release 1 supports hive and postgres. An unknown engine must fail here, not inside a driver
    with a confusing stack trace."""
    with pytest.raises(ConnectionError_, match="kind"):
        _conn(kind="teradata")


def test_the_execution_principal_is_recorded():
    """Which account read the data is part of the evidence, not an operational detail — it decides
    what the read was allowed to see."""
    assert _conn().execution_principal == "svc_featuregen_ro"


def test_a_connection_requires_an_execution_principal():
    with pytest.raises(ConnectionError_, match="principal"):
        _conn(execution_principal="")
