from __future__ import annotations


def test_core_interface_functions_importable_from_contracts():
    # The overview declares these AUTHORITATIVE functions live in featuregen.contracts and are
    # imported by every phase. Downstream phases must be able to import them from here.
    from featuregen.contracts import (
        append_event,
        load_stream,
        projection_lag,
        rebuild_projection,
        run_projection,
    )

    for fn in (append_event, load_stream, run_projection, rebuild_projection, projection_lag):
        assert callable(fn)


def test_reexported_functions_are_the_same_objects():
    import featuregen.contracts as contracts
    from featuregen.events.store import append_event as store_append
    from featuregen.projections.runner import run_projection as runner_run

    assert contracts.append_event is store_append
    assert contracts.run_projection is runner_run
