from featuregen.overlay.upload.brake import large_change_brake
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.upload_catalog import UploadCatalog


def _seed_snapshot(db, source, tables):
    for t in tables:
        db.execute(
            "INSERT INTO overlay_catalog_object (catalog_source, object_ref, native_oid, "
            "columns_fingerprint, type_fingerprint, updated_at) "
            "VALUES (%s, %s, NULL, NULL, NULL, now()) "
            "ON CONFLICT (catalog_source, object_ref) DO NOTHING",
            (source, f"public.{t}"))


def _upload(source, tables):
    rows = [CanonicalRow(source, t, "id", "integer") for t in tables]
    return UploadCatalog(source, rows)


def test_first_upload_soft_gates(db):
    res = large_change_brake(db, "deposits", _upload("deposits", ["accounts"]))
    assert res.is_first_upload is True
    assert res.held is False


def test_normal_change_not_held(db):
    _seed_snapshot(db, "deposits", [f"t{i}" for i in range(10)])
    res = large_change_brake(db, "deposits", _upload("deposits", [f"t{i}" for i in range(9)]))
    assert res.held is False


def test_truncated_upload_is_held(db):
    _seed_snapshot(db, "deposits", [f"t{i}" for i in range(10)])
    res = large_change_brake(db, "deposits", _upload("deposits", ["t0", "t1"]))  # 80% removed
    assert res.held is True
    assert "remov" in res.reason.lower()


def test_wrong_source_low_overlap_is_held(db):
    _seed_snapshot(db, "deposits", [f"t{i}" for i in range(10)])
    res = large_change_brake(db, "deposits", _upload("deposits", [f"x{i}" for i in range(10)]))
    assert res.held is True
