"""Task B1 — the frozen plan envelope becomes a real column (migration 1066).

Four properties, and the fourth is the one CI cannot discover on its own:

* the column and the ``dataset_story`` compatibility copy carry the SAME plan, byte for byte;
* a row written WITHOUT the column (every row that predates 1066) still loads its read set;
* the stored plan is sealed — it must hash to the decision manifest's ``binding_plan_hash``, and
  a row where the two differ is refused at load rather than read;
* **the migration is audited against a POPULATED table.** The standing lesson
  (``migration-audits-blind-to-legacy-data``): a fresh-database CI run proves nothing about an
  ALTER that has to land on rows written months ago, and 1063's append-only triggers are exactly
  the kind of thing that could turn a column addition into an exception nobody saw coming.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.feature_planning_contracts import (
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.field_resolution import canonical_hash
from featuregen.overlay.upload.recipe_planning_lens import (
    DatasetStoryV1,
    V2RecipeCandidateV1,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.semantic_option_decision import (
    OptionDecisionIntegrityError,
    decision_facts_for_candidate,
    frozen_binding_plan,
    load_frozen_option_facts,
    persist_option_decisions,
)

RECIPE = "posted_debit_amount"

#: A real envelope shape — the nine keys ``fold_frozen_binding_plan`` returns, nothing invented.
PLAN = {
    "plan_kind": "single_source",
    "catalog_source": "bank",
    "source_table": "txns",
    "population_ref": "txns",
    "read_set": ["public.txns.acct_id", "public.txns.event_ts", "public.txns.txn_amt"],
    "role_bindings": {"amount": "public.txns.txn_amt", "grain": "public.txns.acct_id"},
    "pit": "trailing 90d observation window over event_time events: (cutoff - 90d, cutoff]",
    "output_grain": "account",
    "window": 90,
}

#: Task S1A-5a — the CROSS-CATALOG shape. Its read set spans catalogs, so every entry carries its
#: own catalog in the canonical ``source::schema.table.column`` form; there is no one
#: ``catalog_source`` for entries to inherit, and inventing one would attribute a foreign column to
#: the wrong catalog.
GOVERNED_PLAN = {
    "plan_kind": "governed_cross_catalog",
    "population_ref": "accounts",
    "read_set": ["bank::public.accounts.acct_id", "crm::sales.customers.cust_id"],
    "role_bindings": {"grain": "bank::public.accounts.acct_id",
                      "who": "crm::sales.customers.cust_id"},
    "pit": "as-of state at the cutoff",
    "output_grain": "account",
    "window": None,
}

#: Exactly the columns ``semantic_option_decision`` had BEFORE migration 1066 — the legacy shape
#: the audit seeds and the shape a rollback would read.
_PRE_1066_COLUMNS = (
    "decision_id", "considered_revision_id", "option_id", "generation_run_id",
    "source_definition_id", "generation_source", "computation_kind", "planning_request_hash",
    "parameter_values", "binding_state", "confirmation_required_roles", "readiness",
    "review_current", "recipe_revision_hash", "validation_status",
    "outstanding_requirement_codes", "has_reviewed_formula_expectation",
    "formula_expectation_revision", "plan_envelope_present", "dataset_story",
    "policy_revision_pins", "metadata_snapshot_id", "observation_id", "context_hash",
    "evidence", "decision_manifest", "recorded_at",
)

_MIGRATION_1066 = (
    Path(__file__).resolve().parents[4]
    / "src/featuregen/db/migrations/1066_semantic_option_decision_binding_plan.sql")


def _candidate(*, binding_plan: dict | None) -> V2RecipeCandidateV1:
    definition = v2_recipe_by_id(RECIPE)
    assert definition is not None
    request = planning_request_from_recipe(definition)
    return V2RecipeCandidateV1(
        recipe_id=RECIPE, relationship="primary", planning_request=request,
        planning_request_hash=planning_request_hash(request),
        recipe_revision_hash="rev", verdicts=(), binding_state="bound",
        readiness=definition.readiness, temporal_pit_text=PLAN["pit"], temporal_blocker="",
        review_current=False, review_missing_roles=(), eligibility={},
        dataset_story=DatasetStoryV1(
            population_ref="txns", population_basis="declared_grain",
            dataset_tables=("txns",), cross_dataset=False, codes=()),
        binding_plan=binding_plan)


def _facts(*, binding_plan: dict | None = None) -> dict:
    candidate = _candidate(binding_plan=binding_plan)
    idea = FeatureIdea(name=RECIPE, description="", derives_from=[], aggregation=None,
                       grain_table=None, source_definition_id=RECIPE)
    return decision_facts_for_candidate(candidate, idea, None, "ctx")


def _freeze(conn, facts: dict, *, key: str) -> tuple[str, str]:
    written = persist_option_decisions(
        conn, considered_revision_id=f"rev_{key}", generation_run_id=f"run_{key}",
        metadata_snapshot_id=None, facts_by_option_id={f"opt_{key}": facts})
    assert written == 1
    return f"rev_{key}", f"opt_{key}"


def _stored(conn, revision_id: str, option_id: str) -> tuple:
    return conn.execute(
        "SELECT binding_plan, binding_plan_hash, dataset_story, decision_manifest "
        "FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (revision_id, option_id)).fetchone()


def test_the_column_and_the_story_carry_the_same_plan(conn):
    """A fresh row writes the envelope twice — the record's own column and the compatibility
    copy in ``dataset_story`` — and the two are the SAME BYTES. They are one object referenced
    twice, never a second derivation, and this is what makes the story copy safe to keep for a
    release and safe to drop after it."""
    revision_id, option_id = _freeze(conn, _facts(binding_plan=PLAN), key="both")
    column, _hash, story, _manifest = _stored(conn, revision_id, option_id)

    assert column is not None, "1066's column must be written for a candidate that folded a plan"
    assert json.dumps(column, sort_keys=True) == json.dumps(story["binding_plan"], sort_keys=True)
    assert column == PLAN


def test_a_candidate_that_folded_no_plan_writes_NULL_not_an_empty_object(conn):
    """Honest absence. ``'{}'`` would read as "a plan exists and it authorizes nothing" — a shape
    ``fold_frozen_binding_plan`` never returns."""
    revision_id, option_id = _freeze(conn, _facts(binding_plan=None), key="noplan")
    column, plan_hash, _story, manifest = _stored(conn, revision_id, option_id)

    assert column is None
    assert plan_hash == ""
    assert manifest["binding_plan_hash"] == ""
    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None and frozen.read_set == () and frozen.plan_envelope_present is False


def test_a_legacy_row_without_the_column_still_loads_its_read_set(conn):
    """The fallback, on a row shaped exactly as every pre-1066 row is: the column NULL, the plan
    in ``dataset_story``, the manifest sealing it. Both the frozen-facts reader and the public
    plan reader must answer from the story."""
    facts = _facts(binding_plan=PLAN)
    legacy = {k: v for k, v in facts.items() if k not in ("binding_plan", "binding_plan_hash")}
    revision_id, option_id = _freeze(conn, legacy, key="legacy")

    column, plan_hash, story, _manifest = _stored(conn, revision_id, option_id)
    assert column is None and plan_hash is None          # the pre-1066 shape, on purpose
    assert story["binding_plan"] == PLAN

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert frozen.read_set == tuple(PLAN["read_set"])
    assert frozen.plan_catalog_source == "bank"
    assert frozen_binding_plan(
        conn, considered_revision_id=revision_id, option_id=option_id) == PLAN


def test_the_column_and_the_legacy_story_read_identically(conn):
    """The two storage generations are not two answers: a column row and a story-only row built
    from the same candidate produce the same frozen facts."""
    facts = _facts(binding_plan=PLAN)
    legacy = {k: v for k, v in facts.items() if k not in ("binding_plan", "binding_plan_hash")}
    new_rev, new_opt = _freeze(conn, facts, key="agree_new")
    old_rev, old_opt = _freeze(conn, legacy, key="agree_old")

    modern = load_frozen_option_facts(conn, considered_revision_id=new_rev, option_id=new_opt)
    legacy_facts = load_frozen_option_facts(conn, considered_revision_id=old_rev,
                                            option_id=old_opt)
    assert modern == legacy_facts


def test_the_column_is_the_source_once_the_story_copy_is_gone(conn):
    """The forward half, and the discriminator for the whole task: a row carrying the column and
    NO story copy — the shape every row has one release from now — still loads its read set. A
    reader that still reached into ``dataset_story`` by string path answers nothing here."""
    facts = _facts(binding_plan=PLAN)
    facts["dataset_story"] = {k: v for k, v in facts["dataset_story"].items()
                              if k != "binding_plan"}
    revision_id, option_id = _freeze(conn, facts, key="story_gone")

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert frozen.read_set == tuple(PLAN["read_set"])
    assert frozen.plan_catalog_source == "bank"
    assert frozen_binding_plan(
        conn, considered_revision_id=revision_id, option_id=option_id) == PLAN


def test_the_plan_hash_matches_the_decision_manifest(conn):
    """The seal: the column's hash, the manifest's ``binding_plan_hash`` and the canonical hash
    of the stored plan are ONE value."""
    revision_id, option_id = _freeze(conn, _facts(binding_plan=PLAN), key="seal")
    column, plan_hash, _story, manifest = _stored(conn, revision_id, option_id)

    assert plan_hash == manifest["binding_plan_hash"] == canonical_hash(column)
    assert len(plan_hash) == 64


def test_a_plan_that_does_not_match_its_seal_is_refused_at_load(conn):
    """A row whose stored plan disagrees with the manifest is REFUSED, not read. The table is
    append-only, so the only way to reach this state is an out-of-band write — precisely when a
    governed reader must stop, because the read set an execution authority is judged against
    would otherwise be taken from bytes nobody can authenticate."""
    facts = _facts(binding_plan=PLAN)
    facts["binding_plan"] = {**PLAN, "source_table": "somewhere_else"}   # the seal is unchanged
    revision_id, option_id = _freeze(conn, facts, key="tampered")

    with pytest.raises(OptionDecisionIntegrityError) as excinfo:
        load_frozen_option_facts(
            conn, considered_revision_id=revision_id, option_id=option_id)
    assert facts["decision_manifest"]["binding_plan_hash"] in str(excinfo.value)

    with pytest.raises(OptionDecisionIntegrityError):
        frozen_binding_plan(conn, considered_revision_id=revision_id, option_id=option_id)


def test_a_tampered_legacy_story_is_refused_too(conn):
    """The guard is not opt-out for the rows that predate the column: the story copy is exactly
    as tamper-worthy as the column, so it is sealed by the same manifest."""
    facts = _facts(binding_plan=PLAN)
    facts.pop("binding_plan")
    facts.pop("binding_plan_hash")
    facts["dataset_story"] = {**facts["dataset_story"],
                              "binding_plan": {**PLAN, "read_set": ["public.txns.pan"]}}
    revision_id, option_id = _freeze(conn, facts, key="legacy_tampered")

    with pytest.raises(OptionDecisionIntegrityError):
        load_frozen_option_facts(
            conn, considered_revision_id=revision_id, option_id=option_id)


def test_a_row_predating_the_manifest_is_read_not_refused(conn):
    """A row written before migration 1065 carries ``decision_manifest = '{}'`` and therefore no
    seal at all. There is nothing to compare it against, so it is read — inventing a hash for it
    here would seal a value this deployment computed rather than the one generation froze."""
    facts = _facts(binding_plan=PLAN)
    facts.pop("binding_plan")
    facts.pop("binding_plan_hash")
    facts["decision_manifest"] = {}
    revision_id, option_id = _freeze(conn, facts, key="unsealed")

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None and frozen.read_set == tuple(PLAN["read_set"])


# ── Task S1A-5a: the governed read set is PARSED, not carried ────────────────────────────────────
#
# A ``governed_cross_catalog`` plan's read set is the input to the per-catalog execution-authority
# floor. Every entry has to be attributed to a catalog before any authority can be measured, and an
# entry nobody can attribute must stop the load — the same posture ``_verified_plan``'s seal check
# already takes, and for the same reason: an execution authority judged against a read set nobody
# can authenticate is not an authority.


def test_a_legacy_single_source_row_reads_BYTE_IDENTICALLY(conn):
    """The compatibility floor of the whole change: a ``single_source`` row keeps every field it
    had, gains its honest ``plan_kind``, and gains NO pairs — its bare refs are attributed by the
    plan's one ``catalog_source``, exactly as before."""
    revision_id, option_id = _freeze(conn, _facts(binding_plan=PLAN), key="s1a5a_legacy")

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert frozen.plan_kind == "single_source"
    assert frozen.read_set_pairs == ()
    assert frozen.read_set == tuple(PLAN["read_set"])
    assert frozen.plan_catalog_source == "bank"


def test_a_row_with_no_plan_at_all_carries_an_empty_plan_kind(conn):
    """Honest absence, not an invented kind: a candidate that folded no plan has no plan kind."""
    revision_id, option_id = _freeze(conn, _facts(binding_plan=None), key="s1a5a_noplan")

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert frozen.plan_kind == "" and frozen.read_set_pairs == ()


def test_a_governed_read_set_is_split_into_catalog_and_object_ref_pairs(conn):
    """The forward half: each canonical entry becomes ``(catalog_source, bare schema.table.column)``
    — the shape the floor needs to ask ONE catalog's resolver about ONE object."""
    revision_id, option_id = _freeze(conn, _facts(binding_plan=GOVERNED_PLAN), key="s1a5a_xcat")

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert frozen.plan_kind == "governed_cross_catalog"
    assert frozen.read_set_pairs == (("bank", "public.accounts.acct_id"),
                                     ("crm", "sales.customers.cust_id"))
    # The raw read set is untouched — the pairs are a projection, never a replacement.
    assert frozen.read_set == tuple(GOVERNED_PLAN["read_set"])


def test_every_stored_entry_produces_exactly_one_pair(conn):
    """Count equality, asserted on a read set carrying a DUPLICATE: nothing is silently deduped,
    dropped or expanded between what was stored and what the floor will measure. A read set that
    quietly shrank would be a floor measured over fewer columns than the plan actually reads."""
    plan = {**GOVERNED_PLAN,
            "read_set": [*GOVERNED_PLAN["read_set"], "bank::public.accounts.acct_id"]}
    revision_id, option_id = _freeze(conn, _facts(binding_plan=plan), key="s1a5a_count")

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert len(frozen.read_set_pairs) == len(plan["read_set"]) == 3


@pytest.mark.parametrize(("bad", "expected_phrase"), [
    ("public.accounts.acct_id", "not a normalized logical_ref"),      # unqualified — no catalog
    ("bank::public.accounts", "must name a column"),                  # a TABLE in a read set
    ("BANK::public.accounts.acct_id", "not in canonical form"),       # a spelling drift
    ("::public.accounts.acct_id", "blank catalog_source"),            # names no catalog at all
    ("bank::public.accounts.acct_id.extra", "not a normalized logical_ref"),
])
def test_ONE_malformed_governed_ref_refuses_the_whole_load(conn, bad, expected_phrase):
    """Fail closed, not "skip the bad one". A read set is the complete statement of what a
    materialization may read; dropping an entry nobody can parse would produce a floor measured
    over a SUBSET and call it the whole. The refusal carries the option key (so an operator knows
    which decision to regenerate) and the parser's own message (so they know what is wrong)."""
    key = "s1a5a_bad"                        # each param runs on its own rolled-back connection
    plan = {**GOVERNED_PLAN, "read_set": ["bank::public.accounts.acct_id", bad]}
    revision_id, option_id = _freeze(conn, _facts(binding_plan=plan), key=key)

    with pytest.raises(OptionDecisionIntegrityError) as excinfo:
        load_frozen_option_facts(
            conn, considered_revision_id=revision_id, option_id=option_id)
    message = str(excinfo.value)
    assert expected_phrase in message
    assert f"{revision_id}/{option_id}" in message
    assert repr(bad) in message


def test_a_single_source_plan_is_NOT_subjected_to_the_governed_parser(conn):
    """The gate is the plan KIND, and this is the test that proves it. A ``single_source`` read
    set holds bare refs — every one of which the governed parser refuses — so a loader that parsed
    unconditionally would refuse every legacy row in the table. Same bytes, opposite outcome."""
    unqualified = {**GOVERNED_PLAN, "plan_kind": "single_source", "catalog_source": "bank",
                   "read_set": ["public.accounts.acct_id"]}
    revision_id, option_id = _freeze(conn, _facts(binding_plan=unqualified), key="s1a5a_bare")

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert frozen.read_set == ("public.accounts.acct_id",)          # raw, unparsed
    assert frozen.plan_kind == "single_source"
    assert frozen.read_set_pairs == ()


def test_a_governed_plan_with_an_empty_read_set_loads_with_no_pairs(conn):
    """Empty is not malformed. It measures NOTHING, which the floor then reports as unevaluated —
    a refusal at load would confuse "authorizes nothing" with "cannot be authenticated"."""
    plan = {**GOVERNED_PLAN, "read_set": []}
    revision_id, option_id = _freeze(conn, _facts(binding_plan=plan), key="s1a5a_empty")

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert frozen.plan_kind == "governed_cross_catalog" and frozen.read_set_pairs == ()


# ── the migration audit (CI is blind to legacy data) ─────────────────────────────────────────────


def _seed_legacy_row(conn, *, key: str, plan: dict) -> None:
    """One row in the EXACT pre-1066 shape, written by naming only the pre-1066 columns."""
    facts = _facts(binding_plan=plan)
    conn.execute(
        f"INSERT INTO semantic_option_decision ({', '.join(_PRE_1066_COLUMNS[:-1])}) "
        f"VALUES ({', '.join(['%s'] * (len(_PRE_1066_COLUMNS) - 1))})",
        (f"sod_{key}", f"rev_{key}", f"opt_{key}", f"run_{key}",
         facts["source_definition_id"], facts["generation_source"], facts["computation_kind"],
         facts["planning_request_hash"], Jsonb(facts["parameter_values"]),
         facts["binding_state"], Jsonb(facts["confirmation_required_roles"]),
         facts["readiness"], facts["review_current"], facts["recipe_revision_hash"],
         facts["validation_status"], Jsonb(facts["outstanding_requirement_codes"]),
         facts["has_reviewed_formula_expectation"], facts["formula_expectation_revision"],
         facts["plan_envelope_present"], Jsonb(facts["dataset_story"]),
         Jsonb(facts["policy_revision_pins"]), None, None, facts["context_hash"],
         Jsonb(facts["evidence"]), Jsonb(facts["decision_manifest"])))


def _row_digest(conn, key: str) -> str:
    """A digest over the PRE-1066 columns only — so "unchanged" survives the columns being
    added, which ``row::text`` would not."""
    columns = ", ".join(_PRE_1066_COLUMNS)
    row = conn.execute(
        f"SELECT md5(ROW({columns})::text) FROM semantic_option_decision "
        f"WHERE option_id = %s", (f"opt_{key}",)).fetchone()
    return row[0]


def test_migration_1066_applies_to_a_POPULATED_legacy_table(conn):
    """THE AUDIT. Drop the two columns, seed rows in the pre-1066 shape, then run the migration's
    own SQL against them — the state a live deployment is actually in.

    What it proves, none of which a fresh-database CI run can:
      1. the ALTER succeeds on a table that already has rows;
      2. **1063's append-only triggers are not reached.** They fire ``BEFORE UPDATE OR DELETE FOR
         EACH ROW``; a nullable column with no default is a catalog-only change, so no row is
         rewritten and no trigger runs. Verified rather than assumed: this is precisely the class
         of thing that has bitten this repository before;
      3. every seeded row's stored bytes are byte-identical afterwards;
      4. the new columns are NULL for them — no backfill of invented values;
      5. those rows still read correctly through the ``dataset_story`` fallback;
      6. the guard still guards: an UPDATE is still refused after the ALTER.
    """
    conn.execute("ALTER TABLE semantic_option_decision "
                 "DROP COLUMN IF EXISTS binding_plan, DROP COLUMN IF EXISTS binding_plan_hash")
    _seed_legacy_row(conn, key="audit_a", plan=PLAN)
    _seed_legacy_row(conn, key="audit_b", plan={**PLAN, "window": 30})
    before = {key: _row_digest(conn, key) for key in ("audit_a", "audit_b")}

    conn.execute(_MIGRATION_1066.read_text())            # (1) the migration's OWN sql, verbatim

    after = {key: _row_digest(conn, key) for key in ("audit_a", "audit_b")}
    assert after == before                               # (2)+(3) nothing rewritten, nothing fired

    rows = conn.execute(
        "SELECT option_id, binding_plan, binding_plan_hash FROM semantic_option_decision "
        "WHERE option_id = ANY(%s) ORDER BY option_id",
        (["opt_audit_a", "opt_audit_b"],)).fetchall()
    assert [(r[1], r[2]) for r in rows] == [(None, None), (None, None)]      # (4) no backfill

    frozen = load_frozen_option_facts(                   # (5) still readable, via the story
        conn, considered_revision_id="rev_audit_a", option_id="opt_audit_a")
    assert frozen is not None and frozen.read_set == tuple(PLAN["read_set"])

    with pytest.raises(Exception) as excinfo:            # (6) the guard still guards
        conn.execute("UPDATE semantic_option_decision SET readiness = 'x' "
                     "WHERE option_id = 'opt_audit_a'")
    assert "append-only" in str(excinfo.value)
    conn.rollback()


def test_migration_1066_is_re_runnable_against_the_columns_it_added(conn):
    """``IF NOT EXISTS`` means a re-applied deploy is a no-op rather than an error — the same
    property ``apply_migrations``'s ledger relies on when a checksum matches."""
    conn.execute(_MIGRATION_1066.read_text())
    conn.execute(_MIGRATION_1066.read_text())
    revision_id, option_id = _freeze(conn, _facts(binding_plan=PLAN), key="rerun")
    assert _stored(conn, revision_id, option_id)[0] == PLAN
