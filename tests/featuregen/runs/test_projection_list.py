"""The run-list projection (spec §12): grouped by intent, keyed on immutable columns.

The envelopes are built directly rather than minted through `mint_test_identity`: the projection
takes an envelope as data (it verifies nothing), and the seeded chains must carry the SAME subject
spelling the caller does, which a directly-built envelope makes explicit."""
from psycopg.types.json import Jsonb
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.projection import list_runs
from featuregen.runs.run_identity import record_run_identity

_ADMIN = IdentityEnvelope(subject="a", actor_kind="human", authenticated=True,
                          auth_method="test", role_claims=("platform_admin",))
_OWNER = IdentityEnvelope(subject="u1", actor_kind="human", authenticated=True,
                          auth_method="test", role_claims=("feature_engineer",))


def _mk(db, run_id, with_identity=True, subject="u1"):
    seed_run_chain(db, run_id=run_id, subject=subject)
    if with_identity:
        env = IdentityEnvelope(subject=subject, actor_kind="human", authenticated=True,
                               auth_method="test", role_claims=())
        record_run_identity(db, run_id, env)


def _ids(page):
    """Run ids in page order, flattened back across the intent groups."""
    return [r["generation_run_id"] for g in page["groups"] for r in g["runs"]]


def test_groups_by_intent_and_marks_pre_spine(db):
    _mk(db, "pl-a", with_identity=True)
    _mk(db, "pl-b", with_identity=False)      # chain seeded but no identity row -> pre-spine
    out = list_runs(db, _ADMIN, limit=50)
    runs = {r["generation_run_id"]: r for g in out["groups"] for r in g["runs"]}
    assert runs["pl-a"]["pre_spine"] is False
    assert runs["pl-b"]["pre_spine"] is True


def test_intent_groups_are_exact_and_never_invent_a_header(db):
    """The GROUPS themselves, not the runs flattened back out of them (spec §12).

    Every other test in this file reads `groups` only to flatten it, so all of them pass against a
    projection that returns one group under a fabricated header. The structure is the contract:
    runs sharing an intent share a group carrying that intent's real hypothesis, and a run with no
    intent gets a group whose header is honestly ABSENT rather than invented."""
    seed_run_chain(db, run_id="zz-1", intent_id="shared-i")
    seed_run_chain(db, run_id="zz-2", intent_id="shared-i")
    # Seeded directly: `seed_run_chain` always mints an intent, and an intent-less run is exactly
    # the case with no header to show.
    db.execute("INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
               "VALUES ('zz-0', NULL, %s, '{}')", (Jsonb({"subject": "u1"}),))
    out = list_runs(db, _ADMIN, limit=50)
    # One transaction, so all three share `created_at` and run id alone orders them: zz-2, zz-1,
    # zz-0. The two shared-intent runs are therefore adjacent and must land in ONE group.
    assert [(g["intent_id"], g["hypothesis"], [r["generation_run_id"] for r in g["runs"]])
            for g in out["groups"]] == [("shared-i", "h", ["zz-2", "zz-1"]),
                                        (None, None, ["zz-0"])]


def test_owner_sees_only_their_runs(db):
    _mk(db, "pl-c", subject="u1")
    _mk(db, "pl-d", subject="someone-else")
    ids = _ids(list_runs(db, _OWNER, limit=50))
    assert "pl-c" in ids and "pl-d" not in ids


def test_keyset_pagination_is_stable(db):
    for i in range(5):
        _mk(db, f"pl-p{i}")
    page1 = list_runs(db, _ADMIN, limit=2)
    assert page1["next_cursor"] is not None
    page2 = list_runs(db, _ADMIN, limit=2, cursor=page1["next_cursor"])
    ids1, ids2 = _ids(page1), _ids(page2)
    assert not set(ids1) & set(ids2)
    # Pinned exactly, and deterministic: every row here is written in ONE transaction, so all five
    # share `now()` and the run-id tie-breaker alone orders them. That makes the page contents the
    # falsifying detail — a keyset over `created_at` ALONE returns an EMPTY page 2 (no row is
    # strictly older than an equal timestamp), which disjointness above would happily accept.
    assert ids1 == ["pl-p4", "pl-p3"] and ids2 == ["pl-p2", "pl-p1"]
    # The owner path binds the visibility fragment's param BEFORE the cursor's two. Splicing them
    # in the wrong order does not raise, it silently matches nothing (`read_policy`'s warning), and
    # this is the only call that binds both param sources at once.
    owned1 = list_runs(db, _OWNER, limit=2)
    owned2 = list_runs(db, _OWNER, limit=2, cursor=owned1["next_cursor"])
    assert _ids(owned1) == ids1 and _ids(owned2) == ids2
