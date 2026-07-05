from datetime import datetime, timezone

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay import facts
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.projection import OverlayProjection, current_fact
from featuregen.overlay.store import append_overlay_event
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.upload_catalog import UploadCatalog
from featuregen.projections.runner import run_projection


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _assert_grain(db, source, table, cols):
    ref = CatalogObjectRef(catalog_source=source, object_kind="table",
                           schema="public", table=table, column=None)
    fk = fact_key(ref, "grain")
    value = {"columns": cols, "is_unique": True}
    draft = append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
        actor=_actor(), expected_version=0, payload={
            "catalog_object_ref": {"catalog_source": source, "object_kind": "table",
                                   "schema": "public", "table": table},
            "object_ref": f"public.{table}", "fact_type": "grain",
            "proposed_value": value, "proposal_fingerprint": "fp", "proposed_by": "upload"})
    append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=_actor(), expected_version=1, payload={
            "value": value, "confirmers": [{"subject": "upload", "role": "data_owner"}],
            "expires_at": None, "confirms_event_id": draft.event_id})
    return fk


def test_stale_without_opening_task(db):
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    # Upload 1: table with a grain on 'id'; establish snapshot.
    rows1 = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True)]
    fk = _assert_grain(db, "deposits", "accounts", ["id"])
    run_projection(db, OverlayProjection())
    detect_catalog_changes(db, UploadCatalog("deposits", rows1), actor=_actor(),
                           now=now, open_reverify=False)
    assert current_fact(db, fk)["status"] == "VERIFIED"

    # Upload 2: the 'id' column is gone -> drift should STALE the grain fact, no task.
    rows2 = [CanonicalRow("deposits", "accounts", "name", "text")]
    changes = detect_catalog_changes(db, UploadCatalog("deposits", rows2), actor=_actor(),
                                     now=now, open_reverify=False)
    run_projection(db, OverlayProjection())
    assert any(c.kind == "drop" for c in changes)
    assert current_fact(db, fk)["status"] == "STALE"
    # No reverify task row was opened for this fact.
    row = db.execute(
        "SELECT count(*) FROM human_tasks WHERE fact_key = %s", (fk,)).fetchone()
    assert row[0] == 0
