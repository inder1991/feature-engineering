from featuregen.contracts import IdentityEnvelope


def service_actor() -> IdentityEnvelope:
    """The platform/service principal SP-2's auditable-LLM calls run as (service:intake-agent)."""
    return IdentityEnvelope(
        subject="service:intake-agent",
        actor_kind="service",
        authenticated=True,
        auth_method="mtls",
        role_claims=("intake-agent",),
        source_of_authority="platform",
        attestation="sp2-intake-agent",
    )
