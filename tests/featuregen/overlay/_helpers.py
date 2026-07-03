"""Shared overlay-test doubles and builders.

Imported by conftest (which re-exports `StubCatalog` and exposes the `catalog` fixture) and by the
individual overlay test modules. Centralizes the several per-file CatalogAdapter doubles that had
drifted into near-duplicates (finding CQ14).
"""

import uuid

from featuregen.overlay.catalog import CatalogFact, CatalogObject
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref


class StubCatalog:
    """In-memory CatalogAdapter test double (stands in for the real FixtureCatalog/PostgresCatalog so
    overlay tests stay decoupled from their constructors).

    Covers the union of what the per-file variants needed:

      * ``objects`` -> ``list_objects()`` / ``fingerprint()`` (profiler scans)
      * ``owners``  -> ``owner_of()``; accepted either as ``set_owner(ref, subject)`` (keyed on the
                       display object_ref string) or as a constructor dict keyed on ``(schema, table)``
                       (the profiler tests) — both keyings are honoured on lookup.
      * ``fact``    -> a constant ``get_fact()`` return (resolve tests, which only ever call get_fact).
    """

    def __init__(self, objects=None, owners=None, fact: CatalogFact | None = None) -> None:
        self._objects = list(objects or [])
        self._owners = dict(owners or {})
        self._fact = fact

    def set_owner(self, ref, subject: str) -> None:
        self._owners[display_object_ref(ref)] = subject

    def owner_of(self, ref):
        key = display_object_ref(ref)
        if key in self._owners:
            return self._owners[key]
        return self._owners.get((ref.schema, ref.table))

    def get_fact(self, ref, fact_type, use_case=None):
        return self._fact

    def list_objects(self):
        return list(self._objects)

    def fingerprint(self):
        return {o.object_ref: o for o in self._objects}


class _SeedRegistry:
    """Stand-in HandlerRegistry for `register_overlay` (SP-1 registers no runtime handlers).

    Mirrors the `_Registry` in test_bootstrap.py so the command-path builder can seed the overlay
    command catalog without pulling in a real runtime registry."""

    def __init__(self) -> None:
        self.handlers: dict = {}

    def register(self, handler) -> None:
        self.handlers[handler.name] = handler


def seed_verified_via_command(
    conn, *, ref, fact_type, value, owner, use_case=None, proposer=None
):
    """Reach a VERIFIED overlay fact through the PUBLIC command path, run the projection, and return
    ``(fact_key, confirmed_event_id)`` — the same contract as test_freshness's hand-seeding
    ``_seed_verified``, but exercised end-to-end via ``execute_command``.

    Wiring mirrors test_bootstrap.py: ``register_overlay()`` + ``seed_authz_policy`` +
    ``seed_overlay_authz`` + ``register_command_authorizer(PolicyAuthorizer())`` + a ``StubCatalog``
    whose owner-of ``ref`` is ``owner``; then a service-actor ``propose_fact`` followed by ``owner``'s
    ``confirm_fact``. Because it drives the REAL ``confirm_fact`` handler, this also arms the
    ``overlay_expiry`` timer (idempotency-keyed on ``fact_key:confirmed_event_id``) and closes the
    confirmation task — so a caller that asserts on the ``timers`` table, needs a bespoke
    ``expires_at``/override value, or seeds a dual-owner ``approved_join`` must keep hand-seeding.
    """
    # Function-local imports keep this doubles-only module import-cheap for callers that never seed.
    from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity

    from featuregen.authz.authorizer import PolicyAuthorizer
    from featuregen.authz.policy import seed_authz_policy
    from featuregen.commands.api import execute_command
    from featuregen.commands.authz_seam import register_command_authorizer
    from featuregen.contracts import Command
    from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
    from featuregen.overlay.catalog import register_catalog_adapter
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.projection import OverlayProjection
    from featuregen.projections.runner import run_projection

    register_overlay(_SeedRegistry())
    seed_authz_policy(conn)
    seed_overlay_authz(conn)
    register_command_authorizer(PolicyAuthorizer())
    cat = StubCatalog()
    cat.set_owner(ref, owner)
    register_catalog_adapter(cat)

    svc = proposer or mint_test_service_identity(
        subject="service:overlay-seed", role_claims=("overlay",), attestation="att-seed"
    )
    owner_actor = mint_test_identity(subject=owner, role_claims=("data_owner",))
    tag = uuid.uuid4().hex[:8]

    proposed = execute_command(
        conn,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": ref, "fact_type": fact_type, "use_case": use_case, "proposed_value": value},
            svc,
            f"ik-seed-propose-{tag}",
        ),
    )
    assert proposed.accepted, proposed.denied_reason
    draft = proposed.produced_event_ids[0]

    confirmed = execute_command(
        conn,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": ref, "fact_type": fact_type, "use_case": use_case, "target_event_id": draft},
            owner_actor,
            f"ik-seed-confirm-{tag}",
        ),
    )
    assert confirmed.accepted, confirmed.denied_reason

    run_projection(conn, OverlayProjection())
    return fact_key(ref, fact_type, use_case), confirmed.produced_event_ids[0]


def catalog_columns(ref: CatalogObjectRef, specs):
    """Build the column ``CatalogObject`` list for ``ref`` from ``(name, data_type)`` pairs."""
    return [
        CatalogObject(
            object_ref=f"{ref.schema}.{ref.table}.{name}",
            object_kind="column",
            schema=ref.schema,
            table=ref.table,
            column=name,
            data_type=data_type,
            native_oid=None,
        )
        for name, data_type in specs
    ]
