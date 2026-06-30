from datetime import UTC, datetime, timedelta

from psycopg.rows import dict_row

from featuregen.overlay.freshness import schedule_expiry


def test_schedule_expiry_arms_overlay_timer_and_is_idempotent(db):
    fire_at = datetime.now(UTC) + timedelta(days=180)
    tid = schedule_expiry(db, "fact_abc", "evt_confirmed_1", fire_at)
    assert tid
    # re-arming for the SAME (fact_key, confirmed_event_id) is a no-op (idempotency_key collision)
    schedule_expiry(db, "fact_abc", "evt_confirmed_1", fire_at)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, aggregate, aggregate_id, payload FROM timers "
            "WHERE kind='overlay_expiry' AND aggregate_id=%s",
            ("fact_abc",),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["aggregate"] == "overlay_fact"
    assert rows[0]["payload"]["confirmed_event_id"] == "evt_confirmed_1"
