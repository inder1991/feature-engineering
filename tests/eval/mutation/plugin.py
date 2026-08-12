"""The pytest plugin that installs one mutation, in the subprocess, before collection.

Loaded with `-p tests.eval.mutation.plugin` and driven by `FEATUREGEN_MUTATION`. `pytest_configure`
runs before test modules are imported, so a module-level `setattr` on the target is in place before
any victim binds anything — which is why the registry patches CONSUMERS rather than producers
wherever a symbol travels by `from X import Y`.

Fails loudly on an unknown id. A mutation harness whose mutation silently did not apply would
report every victim as surviving and read as a passing gate, which is the one outcome worse than a
red one.
"""
from __future__ import annotations

import os

ENV_VAR = "FEATUREGEN_MUTATION"


def pytest_configure(config) -> None:
    mutation_id = os.environ.get(ENV_VAR, "").strip()
    if not mutation_id:
        return
    from tests.eval.mutation.mutations import BY_ID

    mutation = BY_ID.get(mutation_id)
    if mutation is None:
        raise RuntimeError(
            f"{ENV_VAR}={mutation_id!r} is not a registered mutation; "
            f"known: {sorted(BY_ID)}")
    mutation.apply()
    print(f"[mutation] applied {mutation.mutation_id} -> {mutation.target}")
