from featuregen.aggregates.ids import mint_id
from featuregen.contracts import Command, IdentityEnvelope


def make_actor(subject="user:raj", actor_kind="human", roles=("data_scientist",)):
    return IdentityEnvelope(
        subject=subject,
        actor_kind=actor_kind,
        authenticated=True,
        auth_method="oidc",
        role_claims=tuple(roles),
    )


def make_cmd(
    action, aggregate, aggregate_id, args, *, actor=None, idem=None, expected_version=None
):
    return Command(
        action=action,
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        args=args,
        actor=actor or make_actor(),
        idempotency_key=idem or mint_id("idem"),
        expected_version=expected_version,
    )
