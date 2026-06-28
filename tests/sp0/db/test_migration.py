from __future__ import annotations


def test_phase07_tables_exist(db):
    rows = db.execute(
        """
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public'
           AND table_name IN ('authz_policy','security_audit','human_tasks',
                              'human_task_responses','task_delegations')
         ORDER BY table_name
        """
    ).fetchall()
    assert [r[0] for r in rows] == [
        "authz_policy",
        "human_task_responses",
        "human_tasks",
        "security_audit",
        "task_delegations",
    ]


def test_prereq_objects_present(db):
    assert db.execute("SELECT nextval('global_seq_seq')").fetchone()[0] >= 1
    assert db.execute("SELECT to_regclass('public.events')").fetchone()[0] == "events"
    assert db.execute("SELECT to_regclass('public.timers')").fetchone()[0] == "timers"


def test_mint_id_prefixes():
    from sp0.idgen import mint_id

    one = mint_id("task")
    two = mint_id("task")
    assert one.startswith("task_")
    assert one != two
