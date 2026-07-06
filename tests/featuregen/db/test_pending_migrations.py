from featuregen.db.migrations import pending_migrations


def test_pending_migrations_empty_on_a_migrated_db(db):
    # the db fixture is fully migrated by the harness -> nothing pending.
    assert pending_migrations(db) == []
