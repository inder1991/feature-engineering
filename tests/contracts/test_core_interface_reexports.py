from __future__ import annotations


def test_core_interface_functions_importable_from_contracts():
    # The overview declares these AUTHORITATIVE functions live in sp0.contracts and are
    # imported by every phase. Downstream phases must be able to import them from here.
    from sp0.contracts import (
        append_event,
        load_stream,
        projection_lag,
        rebuild_projection,
        run_projection,
    )

    for fn in (append_event, load_stream, run_projection, rebuild_projection, projection_lag):
        assert callable(fn)


def test_reexported_functions_are_the_same_objects():
    import sp0.contracts as contracts
    from sp0.events.store import append_event as store_append
    from sp0.projections.runner import run_projection as runner_run

    assert contracts.append_event is store_append
    assert contracts.run_projection is runner_run
