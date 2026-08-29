"""§0.10 step 3 — declare a build set, ask to build it, watch the attempt.

The rules under test are the ones that cost compute or mislead a person:

1. **The route never runs the chain.** It records, enqueues and returns — an AST check, because a
   route holds one transaction for its whole request and a generation is not a request-shaped
   amount of work.
2. **A double-click does not start a second compile**, and the second answer says so.
3. **The environment comes from the AUTHORIZATION**, never from the body.
4. **The request and its queue row commit together** — neither exists without the other.
5. **Flag off is a 404**, not a 503: this deployment does not run V2 generation at all.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from tests.featuregen.materialize.crosswalk_fixtures import (
    bind_ready_formulas,
    build_set_declaration,
)
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.materialize.build_set_declaration import encode_declaration

SETS = "/build-sets"
GENERATIONS = "/build-sets/generations"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")


@pytest.fixture
def engineer_headers():
    """The role that MAY generate. Administering the platform is a different permission from
    running a build, and the route gates on the latter."""
    return {"X-User": "sam", "X-Roles": "feature_engineer"}


def _seed(conn, *, environment="hdfc-local", group="customer_txn_features",
          authorized_by="user:sam"):
    """A target reading, two selections, a recorded build set and an approval for it.

    ▲ `authorized_by` DEFAULTS TO THE CALLER (`user:sam`, the subject `engineer_headers` resolves
    to). It used to default to `user:ops` while every request ran as `sam`, so the whole suite was
    exercising a caller spending an authorization issued to somebody else — and asserting it
    succeeded. The parameter stays so that refusal is itself testable.
    """
    from featuregen.materialize.generation_authorization import (
        GenerationAuthorizationV1,
        record_generation_authorization,
    )
    from featuregen.overlay.upload.build_set_store import record_build_set
    from featuregen.overlay.upload.selection_revisions import TargetModeV1

    # Migration 1116 makes `feature_selection_revision.considered_revision_id` a real foreign key,
    # so `crev-bs` has to exist. Seeding only, and uncommitted like everything else here.
    seed_run_chain(conn, run_id="bsapi", considered_revision_id="crev-bs")
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, redacted_hypothesis) "
        "VALUES ('int-bs','h','hypothesis','h') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES ('trr-bs','int-bs','exploration','sha256:t') ON CONFLICT DO NOTHING")
    for name in ("sel-1", "sel-2"):
        conn.execute(
            "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
            "considered_revision_id, option_id, decision_id, planning_request_hash, "
            "binding_plan_hash, content_hash) "
            "VALUES (%s,'trr-bs','crev-bs',%s,%s,'sha256:a','sha256:b',%s) "
            "ON CONFLICT DO NOTHING",
            (name, f"opt-{name}", f"dec-{name}", f"sha256:{name}"))
    build_set, _ = record_build_set(
        conn, revision_id="bs-api", target_reading_revision_id="trr-bs",
        selection_formula_binding_ids=list(bind_ready_formulas(conn, ["sel-1", "sel-2"])),
        declaration=build_set_declaration(),
        declared_by="user:ops", declared_at="2026-08-21T00:00:00Z")
    approval = record_generation_authorization(
        conn, GenerationAuthorizationV1(
            environment_id=environment, logical_group_name=group,
            build_set_revision_id=build_set,
            target_mode=TargetModeV1.EXPLORATION, target_ref=None),
        authorized_by=authorized_by, authorized_at="2026-08-21T00:00:00Z")
    # NO COMMIT. `make_client` overrides `get_conn` with this very connection, so the routes see
    # these rows uncommitted — and the root `conn` fixture rolls the whole thing back on teardown.
    # Committing here would make the seed permanent and leak `sel-1`, `bs-api` and an approval into
    # every suite that runs after this one.
    return build_set, approval


def _body(build_set, approval, **overrides):
    body = {
        "build_set_revision_id": build_set,
        "generation_authorization_revision_id": approval,
        "physical_type_policy": "formula-v2/physical-types@1",
        "empty_values": {"posted_amount_30d": "0"},
        "engine_id": "kedro-pyspark",
    }
    body.update(overrides)
    return body


# ══ THE ROUTE NEVER RUNS THE CHAIN ═════════════════════════════════════════════════════════════
def test_the_route_module_NEVER_NAMES_THE_CHAIN() -> None:
    """Read as source, not asserted by behaviour, because the failure this prevents is a future
    edit rather than a current bug: `get_conn` holds ONE transaction for the whole request, and a
    generation restores, admits, compiles, renders and seals. A route that called any of it would
    hold that transaction for the duration and time out under a real build set.
    """
    source = pathlib.Path("src/featuregen/api/routes/build_sets.py").read_text()
    names = {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}

    forbidden = {"compile_generation_v2", "generate_v2", "seal_v2", "render_project",
                 "assemble_nodes", "process_generation_once", "admit_artifacts_v2"}
    assert not (names & forbidden), sorted(names & forbidden)


# ══ DECLARING ══════════════════════════════════════════════════════════════════════════════════
def test_A_BUILD_SET_IS_DECLARED_and_returned(client, conn, enabled, engineer_headers) -> None:
    _seed(conn)

    response = client.post(SETS, json={
        "target_reading_revision_id": "trr-bs",
        "selection_formula_binding_ids": list(bind_ready_formulas(conn, ["sel-1", "sel-2"])),
        "declaration": encode_declaration(build_set_declaration())}, headers=engineer_headers)

    assert response.status_code == 201, response.text
    assert response.json()["selection_revision_ids"] == ["sel-1", "sel-2"]


def test_DECLARING_THE_SAME_BUILD_TWICE_IS_ONE_SET(client, conn, enabled,
                                                   engineer_headers) -> None:
    """Idempotent on CONTENT. A second identical set would split its attempts across two roots and
    make "how did this build go" a question with two answers."""
    _seed(conn)
    payload = {"target_reading_revision_id": "trr-bs",
               "selection_formula_binding_ids": list(bind_ready_formulas(conn, ["sel-1", "sel-2"])),
               "declaration": encode_declaration(build_set_declaration())}

    first = client.post(SETS, json=payload, headers=engineer_headers).json()
    second = client.post(SETS, json=payload, headers=engineer_headers).json()

    assert second["build_set_revision_id"] == first["build_set_revision_id"]
    assert second["created"] is False


def test_an_EMPTY_BUILD_SET_is_422_and_says_why(client, conn, enabled, engineer_headers) -> None:
    _seed(conn)

    response = client.post(SETS, json={
        "target_reading_revision_id": "trr-bs", "selection_formula_binding_ids": []},
        headers=engineer_headers)

    assert response.status_code == 422, response.text


# ══ ASKING TO BUILD ════════════════════════════════════════════════════════════════════════════
def test_REQUESTING_A_BUILD_QUEUES_IT(client, conn, enabled, engineer_headers) -> None:
    """202 and not 201: the artifact does not exist when this returns."""
    build_set, approval = _seed(conn)

    response = client.post(GENERATIONS, json=_body(build_set, approval),
                           headers=engineer_headers)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created"] is True
    assert body["environment_id"] == "hdfc-local"

    queued = conn.execute(
        "SELECT count(*) FROM queue WHERE message_id = %s",
        (f"generation:{body['request_id']}",)).fetchone()[0]
    assert queued == 1, "the request and its work item must commit together"


def test_A_DOUBLE_CLICK_DOES_NOT_START_A_SECOND_COMPILE(client, conn, enabled,
                                                        engineer_headers) -> None:
    """Idempotency is on the WORK, so a client minting a fresh key per click cannot defeat it."""
    build_set, approval = _seed(conn)

    first = client.post(GENERATIONS, json=_body(build_set, approval),
                        headers=engineer_headers).json()
    second = client.post(GENERATIONS, json=_body(build_set, approval),
                         headers=engineer_headers).json()

    assert second["request_id"] == first["request_id"]
    assert second["created"] is False
    assert conn.execute("SELECT count(*) FROM generation_request").fetchone()[0] == 1


def test_the_ENVIRONMENT_COMES_FROM_THE_APPROVAL_not_the_body(client, conn, enabled,
                                                              engineer_headers) -> None:
    """A caller-supplied environment would let a build authorized for one cluster be requested
    against another — and the composite key would then refuse it with a message about keys rather
    than about permission."""
    build_set, approval = _seed(conn, environment="hdfc-prod")

    response = client.post(
        GENERATIONS,
        json={**_body(build_set, approval), "environment_id": "somewhere-else"},
        headers=engineer_headers)

    # `extra="forbid"`: the field does not exist, so supplying it is a 422 rather than being
    # silently ignored — an ignored field reads as accepted.
    assert response.status_code == 422, response.text

    accepted = client.post(GENERATIONS, json=_body(build_set, approval),
                           headers=engineer_headers)
    assert accepted.json()["environment_id"] == "hdfc-prod"


def test_an_APPROVAL_FOR_A_DIFFERENT_SET_IS_REFUSED(client, conn, enabled,
                                                    engineer_headers) -> None:
    """409 rather than 422: both halves exist and are individually valid, and what is wrong is the
    relation between them."""
    build_set, approval = _seed(conn)
    from featuregen.overlay.upload.build_set_store import record_build_set
    other, _ = record_build_set(
        conn, revision_id="bs-other", target_reading_revision_id="trr-bs",
        selection_formula_binding_ids=list(bind_ready_formulas(conn, ["sel-2", "sel-1"])),
        declaration=build_set_declaration(),
        declared_by="user:ops", declared_at="2026-08-21T00:00:00Z")

    response = client.post(GENERATIONS, json=_body(other, approval), headers=engineer_headers)

    assert response.status_code == 409, response.text
    assert "approves build set" in response.json()["detail"]


def test_an_APPROVAL_THAT_DOES_NOT_EXIST_is_404(client, conn, enabled, engineer_headers) -> None:
    build_set, _ = _seed(conn)

    response = client.post(GENERATIONS, json=_body(build_set, "gar-nope"),
                           headers=engineer_headers)

    assert response.status_code == 404, response.text


# ══ WATCHING ═══════════════════════════════════════════════════════════════════════════════════
def test_AN_ATTEMPT_CAN_BE_READ_BACK_with_server_owned_words(client, conn, enabled,
                                                             engineer_headers) -> None:
    """`stage_label` is server-owned: a screen that mapped statuses to words itself would be a
    second vocabulary, and the two would disagree the first time either changed."""
    build_set, approval = _seed(conn)
    request_id = client.post(GENERATIONS, json=_body(build_set, approval),
                             headers=engineer_headers).json()["request_id"]

    response = client.get(f"{GENERATIONS}/{request_id}", headers=engineer_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "REQUESTED"
    assert body["stage_label"] == "Queued"
    assert body["sealed_artifact_id"] is None
    assert body["refusals"] == []


def test_an_attempt_that_does_not_exist_is_404(client, enabled, engineer_headers) -> None:
    assert client.get(f"{GENERATIONS}/nope", headers=engineer_headers).status_code == 404


# ══ THE SWITCH ═════════════════════════════════════════════════════════════════════════════════
def test_a_FLAG_OFF_DEPLOYMENT_ANSWERS_404_on_every_path(client, monkeypatch,
                                                         engineer_headers) -> None:
    """Not 503. A 503 says "this exists and is unwell"; the flag says this deployment does not run
    V2 generation at all."""
    monkeypatch.delenv("FEATUREGEN_GENERATION_V2_ENABLED", raising=False)

    assert client.post(SETS, json={"target_reading_revision_id": "t",
                                   "selection_formula_binding_ids": ["b"]},
                       headers=engineer_headers).status_code == 404
    assert client.post(GENERATIONS, json=_body("bs", "gar"),
                       headers=engineer_headers).status_code == 404
    assert client.get(f"{GENERATIONS}/req-1", headers=engineer_headers).status_code == 404


# ══ THE DECLARATION REACHES THE JOB ════════════════════════════════════════════════════════════
def test_the_QUEUED_JOB_CARRIES_THE_SETS_DECLARATION_not_nulls(client, conn, enabled,
                                                               engineer_headers) -> None:
    """▲ THE HOLE THIS CLOSES. The route used to send `spine_declaration=None`, `cadence=None`,
    `availability_promise=None` and `operand_facts={}` — honest, because the lane refuses such a job
    BY NAME rather than inventing values, but it meant a build requested through the API could never
    run. The five declarations now come from the SET, which is where they were always declared.

    Asserted on the QUEUE PAYLOAD rather than on a 202, because a route can return success while
    enqueuing a job nothing can execute, and that is precisely what it used to do.
    """
    import json as _json

    from featuregen.materialize.generation_lane import decode_job

    build_set, approval = _seed(conn)

    response = client.post(GENERATIONS, json=_body(build_set, approval),
                           headers=engineer_headers)
    assert response.status_code == 202, response.text

    payload = conn.execute(
        "SELECT payload FROM queue WHERE handler LIKE '%generation%' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    job = decode_job(payload if isinstance(payload, dict) else _json.loads(payload))

    declared = build_set_declaration()
    assert job.spine_declaration == declared.spine_declaration
    assert job.cadence == declared.cadence
    assert job.availability_promise == declared.availability_promise


def test_a_DECLARATION_THIS_BUILD_CANNOT_READ_IS_REFUSED_not_half_read(client, conn, enabled,
                                                                      engineer_headers) -> None:
    """A payload under an unknown encoding is refused WHOLE. Reading the half a build understands
    is how a generation silently computes the wrong population."""
    payload = {**encode_declaration(build_set_declaration()), "version": 99}

    # ▲ Placeholder pins on purpose: the declaration is decoded BEFORE any member is resolved, so
    # seeding real bindings here would be building a world the assertion never reaches — and it
    # would hide a regression that moved the decode after the member lookup.
    response = client.post(SETS, json={
        "target_reading_revision_id": "trr-bs",
        "selection_formula_binding_ids": ["b-unused-1", "b-unused-2"],
        "declaration": payload}, headers=engineer_headers)

    assert response.status_code == 422, response.text
    assert "refused rather than read in part" in response.text


def test_an_INCOMPLETE_DECLARATION_IS_REFUSED_by_name(client, conn, enabled,
                                                      engineer_headers) -> None:
    """Each missing field decides a published number, so the refusal names what is absent rather
    than reporting a generic validation failure."""
    payload = encode_declaration(build_set_declaration())
    payload.pop("cadence")

    # ▲ Placeholder pins on purpose: the declaration is decoded BEFORE any member is resolved, so
    # seeding real bindings here would be building a world the assertion never reaches — and it
    # would hide a regression that moved the decode after the member lookup.
    response = client.post(SETS, json={
        "target_reading_revision_id": "trr-bs",
        "selection_formula_binding_ids": ["b-unused-1", "b-unused-2"],
        "declaration": payload}, headers=engineer_headers)

    assert response.status_code == 422, response.text
    assert "cadence" in response.text


def test_AN_APPROVAL_GRANTED_TO_SOMEBODY_ELSE_CANNOT_BE_SPENT(
        client, conn, enabled, engineer_headers) -> None:
    """▲ The hole this closes: the route proved the approval covered the BUILD SET and never asked
    who might SPEND it.

    Any holder of `feature:generate` could name another person's approval and build under it. The
    request would be recorded as theirs while consuming an approval nobody agreed they could use —
    so the audit trail would read as if that were intended.

    ▲ This is NOT segregation of duties, and it must not be read as the beginning of it. The
    development policy is deliberately permissive — any authenticated development user may trigger
    any implemented non-production stage. What this pins is the half that is NOT temporary:
    permission is server-owned, so a caller cannot spend an authorization the server did not issue
    to them. The eventual form removes the request field entirely and has the server resolve it.
    """
    build_set, approval = _seed(conn, authorized_by="user:ops")

    response = client.post(GENERATIONS, json=_body(build_set, approval),
                           headers=engineer_headers)

    assert response.status_code == 403, response.text
    body = response.json()["detail"]
    assert body["code"] == "ACTION_AUTHORIZATION_NOT_HELD"
    # It NAMES the grantee: "you may not" without "who may" sends somebody to the wrong person.
    assert "user:ops" in body["detail"]


def test_THE_REFUSED_BUILD_QUEUES_NOTHING(
        client, conn, enabled, engineer_headers) -> None:
    """▲ Asserted on the QUEUE, not on the response. A refusal that still enqueued would be a
    refusal in the reply and a build in fact — and the reply is not what spends the compute."""
    build_set, approval = _seed(conn, authorized_by="user:ops")
    before = conn.execute("SELECT count(*) FROM queue").fetchone()[0]

    client.post(GENERATIONS, json=_body(build_set, approval), headers=engineer_headers)

    after = conn.execute("SELECT count(*) FROM queue").fetchone()[0]
    assert after == before


def test_A_QUEUED_BUILD_CARRIES_ITS_RECORDED_DECISION(client, conn, enabled,
                                                      engineer_headers) -> None:
    """▲ §8.2's first moment, asserted at the route: the decision is DURABLE (migration 1106) and
    its id is frozen into the queue payload — which is what gives the worker's second look something
    to compare against. A decision that lived only in the response would be gone by then."""
    build_set, approval = _seed(conn)

    response = client.post(GENERATIONS, json=_body(build_set, approval),
                           headers=engineer_headers)

    assert response.status_code == 202, response.text
    payload = conn.execute(
        "SELECT payload FROM queue WHERE message_id = %s",
        (f"generation:{response.json()['request_id']}",)).fetchone()[0]
    decision_id = payload["action_decision_revision_id"]
    assert decision_id, "the frozen job names its decision"

    stored = conn.execute(
        "SELECT action, resource_identity_hash, allowed FROM action_decision_revision "
        "WHERE decision_id = %s", (decision_id,)).fetchone()
    assert stored == ("GENERATE_PREVIEW", build_set, True)
    # And the authorization it binds is SERVER-owned: minted from the authenticated identity,
    # never a request field.
    actor = conn.execute(
        "SELECT a.actor_subject FROM action_authorization_revision a "
        "JOIN action_decision_revision d ON d.authorization_id = a.authorization_id "
        "WHERE d.decision_id = %s", (decision_id,)).fetchone()[0]
    assert actor == "user:sam"


# ══ B0a — THE READ SCOPE IS THE SERVER'S ANSWER, NEVER THE CALLER'S CLAIM ══════════════════════
def test_a_CLIENT_ROLE_CLAIM_IS_REFUSED_BY_NAME(client, conn, enabled, engineer_headers) -> None:
    """▲ THE HOLE. `roles` was a field on this body, taken verbatim into the queue payload and on
    to `decide_read_scope` — so a caller holding `feature:generate` could claim read roles it was
    never granted, and a cross-catalog build makes that reach across catalogs.

    Refused rather than ignored: an API that silently drops an authority claim leaves the caller
    believing it was honoured.
    """
    build_set, approval = _seed(conn)

    response = client.post(
        GENERATIONS,
        json={**_body(build_set, approval), "roles": ["pii_reader", "restricted_reader"]},
        headers=engineer_headers)

    assert response.status_code == 422, response.text
    assert "roles" in str(response.json()["detail"])
    assert conn.execute("SELECT count(*) FROM generation_request").fetchone()[0] == 0


def test_the_QUEUED_SCOPE_IS_THE_AUTHENTICATED_PRINCIPALS(client, conn, enabled,
                                                          engineer_headers) -> None:
    """What reaches generation is what `get_identity` resolved — frozen, and named by the payload
    only as a binding id. There is nowhere in the work item for a claim to live."""
    from featuregen.identity.principal_scope import (
        load_principal_scope_binding,
        load_principal_scope_revision,
    )

    build_set, approval = _seed(conn)

    body = client.post(GENERATIONS, json=_body(build_set, approval),
                       headers=engineer_headers).json()

    payload = conn.execute("SELECT payload FROM queue WHERE message_id = %s",
                           (f"generation:{body['request_id']}",)).fetchone()[0]
    assert "roles" not in payload
    binding = load_principal_scope_binding(
        conn, subject_kind="generation_request", subject_id=body["request_id"])
    assert binding is not None
    assert payload["principal_scope_binding_id"] == binding.binding_id
    # The DECISION this scope was frozen against, on the binding itself — so "who authorized this,
    # under which verdict" is one row rather than a join somebody has to guess at.
    assert binding.action_decision_revision_id is not None
    revision = load_principal_scope_revision(conn, binding.revision_id)
    assert revision is not None
    assert revision.subject == "user:sam"
    assert revision.role_claims == ("feature_engineer",)


# ══ B0a fix round — THE DECLARER IS SERVER-OWNED TOO, and the payload carries no client claim ═══
def _forged_declaration() -> dict:
    """A declaration whose spine names an identity the caller is not: authenticated, break-glass,
    and holding two roles it was never granted.

    Every field of `declared_by` is client JSON — `queue_lane._decode_spine` rebuilds the envelope
    with `cls(**node)` behind a field-name check, and `IdentityEnvelope` is a plain frozen dataclass
    with no validation of its own. `declared_by` is REQUIRED, so every POST already carries a
    client-authored identity; this one simply makes it a hostile one.
    """
    import dataclasses

    from tests.featuregen.materialize.crosswalk_fixtures import SPINE_DECLARATION

    from featuregen.contracts.envelopes import IdentityEnvelope

    return encode_declaration(build_set_declaration(
        spine_declaration=dataclasses.replace(
            SPINE_DECLARATION,
            declared_by=IdentityEnvelope(
                subject="user:mallory", actor_kind="human", authenticated=True,
                auth_method="password", role_claims=("pii_reader", "platform_admin"),
                break_glass=True))))


def _declared_through_the_route(client, conn, headers) -> str:
    """A build set declared the way production declares one — through the route, with a hostile
    `declared_by` inside its declaration. ONE member, so its content differs from `_seed`'s set:
    provenance is outside the identity, so a two-member forged declaration would land on the
    existing row (correctly) and prove nothing about what this call stored."""
    from tests.featuregen.materialize.crosswalk_fixtures import bind_ready_formulas

    _seed(conn)
    response = client.post(SETS, json={
        "target_reading_revision_id": "trr-bs",
        "selection_formula_binding_ids": list(bind_ready_formulas(conn, ["sel-1"])),
        "declaration": _forged_declaration()}, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["created"] is True
    return response.json()["build_set_revision_id"]


def _stored_declaration(conn, revision_id: str) -> dict:
    import json as _json

    stored = conn.execute(
        "SELECT declaration_json FROM build_set_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()[0]
    return stored if isinstance(stored, dict) else _json.loads(stored)


def _walk(node, path=""):
    """Every (path, key, value) in a payload — so a claim cannot hide one level down."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}", key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def test_a_FORGED_DECLARER_IS_OVERWRITTEN_by_the_authenticated_principal(client, conn, enabled,
                                                                        engineer_headers) -> None:
    """▲ THE SAME SHAPE AS `roles`, ONE KEY LOWER — and closed the same way. A caller declaring
    `authenticated=True`, `break_glass=True` and two roles it was never granted has that envelope
    replaced by the one `get_identity` resolved. It reaches no read decision today (provenance only,
    excluded from `identity_payload`), which is exactly why it is closed before it does.
    """
    revision_id = _declared_through_the_route(client, conn, engineer_headers)

    declarer = _stored_declaration(conn, revision_id)["spine_declaration"]["declared_by"]

    assert declarer["subject"] == "user:sam", "the declarer is the authenticated principal"
    assert declarer["role_claims"] == ["feature_engineer"]
    assert declarer["break_glass"] is False
    assert declarer["authenticated"] is False, (
        "the dev stub ASSERTS an identity rather than proving one, and the record says so rather "
        "than taking the caller's word that it was authenticated")


def test_OVERWRITING_THE_DECLARER_CHANGES_NO_IDENTITY(client, conn, enabled,
                                                      engineer_headers) -> None:
    """Provenance is outside the hash, so this closure re-ids nothing: the same declaration sent by
    two different principals is still ONE build set, which is the property `identity_payload`
    exists to protect."""
    first = _declared_through_the_route(client, conn, engineer_headers)

    from tests.featuregen.materialize.crosswalk_fixtures import bind_ready_formulas

    again = client.post(SETS, json={
        "target_reading_revision_id": "trr-bs",
        "selection_formula_binding_ids": list(bind_ready_formulas(conn, ["sel-1"])),
        "declaration": _forged_declaration()},
        headers={"X-User": "riya", "X-Roles": "feature_engineer"})

    assert again.json()["build_set_revision_id"] == first
    assert again.json()["created"] is False
    # And the FIRST declarer's provenance stands — a second declaration of identical content adds
    # nothing, so it does not get to re-attribute the first.
    declarer = _stored_declaration(conn, first)["spine_declaration"]["declared_by"]
    assert declarer["subject"] == "user:sam"


def test_the_QUEUE_PAYLOAD_CARRIES_NO_CLIENT_AUTHORED_CLAIM_ANYWHERE(client, conn, enabled,
                                                                    engineer_headers) -> None:
    """WALKED, not spot-checked at the top level. `roles` is gone from the payload at every depth,
    and the one nested identity it does carry — the spine's `declared_by` provenance — is the
    server's answer rather than the caller's."""
    from featuregen.materialize.generation_authorization import (
        GenerationAuthorizationV1,
        record_generation_authorization,
    )
    from featuregen.overlay.upload.selection_revisions import TargetModeV1

    build_set = _declared_through_the_route(client, conn, engineer_headers)
    approval = record_generation_authorization(
        conn, GenerationAuthorizationV1(
            environment_id="hdfc-local", logical_group_name="customer_txn_features",
            build_set_revision_id=build_set, target_mode=TargetModeV1.EXPLORATION,
            target_ref=None),
        authorized_by="user:sam", authorized_at="2026-08-21T00:00:00Z")

    body = client.post(GENERATIONS, json=_body(build_set, approval),
                       headers=engineer_headers).json()

    payload = conn.execute("SELECT payload FROM queue WHERE message_id = %s",
                           (f"generation:{body['request_id']}",)).fetchone()[0]
    assert not [path for path, key, _ in _walk(payload) if key == "roles"], (
        "no `roles` key at ANY depth — read scope travels as a binding id")
    claims = sorted(path for path, key, _ in _walk(payload)
                    if key in {"role_claims", "authenticated", "break_glass"})
    assert claims == [
        ".spine_declaration.declared_by.authenticated",
        ".spine_declaration.declared_by.break_glass",
        ".spine_declaration.declared_by.role_claims",
    ], f"an unexpected identity claim rides the payload: {claims}"
    declarer = payload["spine_declaration"]["declared_by"]
    assert declarer["subject"] == "user:sam"
    assert declarer["role_claims"] == ["feature_engineer"]
    assert declarer["break_glass"] is False
