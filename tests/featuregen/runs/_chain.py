"""Seed one complete intent -> recognition -> run -> scope -> input -> considered -> snapshot chain.

Every parent 1115's composite FKs reference, with minimal NOT NULL columns, so a test can mint a
chain in one call. Ids default from run_id so two chains never collide."""
from psycopg.types.json import Jsonb


def feature_idea(name, grain="deposits"):
    """One `FeatureIdea` that resolves to a real authoring strategy — the LLM lane, since nothing
    seeds a reviewed v2 expectation for it."""
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    return FeatureIdea(name=name, description=f"{name} over {grain}",
                       derives_from=[f"{grain}.balance"], aggregation="avg", grain_table=grain)


def considered_json(options):
    """A considered revision carrying SEVERAL options in the shipped canonical v3 shape.

    Assembled with the production helpers (`_candidate_identity`, `_idea_json`) rather than
    hand-written, for `test_formula_drafts._revision`'s reason: the resolver cross-checks the
    private identity against its public projection, so a hand-written pair only proves that two
    hand-written things agree. The first option is the ANCHOR and the rest are alternatives, which
    is the only shape `_public_option_entries` reads.

    Shared here rather than copied per test module: two spellings of one frozen payload is two
    fixtures to keep in step with a resolver that cross-checks them.
    """
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.contract.gate1 import _candidate_identity, _idea_json

    public_anchor, options_by_id, alternatives = None, {}, []
    for position, (option_id, idea) in enumerate(options):
        source, lens = ("anchor", "anchor") if position == 0 else ("alternative", "deposits")
        identity = _candidate_identity(path=f"p{position}", source=source, lens=lens, feature=idea)
        feature = {**_idea_json(idea), "option_id": option_id}
        if position == 0:
            public_anchor = feature
        else:
            alternatives.append(feature)
        options_by_id[option_id] = {
            "source": source, "lens": lens,
            "canonical_candidate_identity": identity,
            "canonical_candidate_identity_hash": canonical_hash(identity),
            "recipe_candidate_key": None,
        }
    public = {"anchor": public_anchor, "rejections": []}
    if alternatives:
        public["alternatives"] = [{"lens": "deposits", "features": alternatives}]
    return {
        "version": "contract-considered-v3",
        "public": public,
        "options_by_id": options_by_id,
        "recipe_grounding_context_by_candidate_key": {},
        "recipe_candidate_keys_by_recipe_id": {},
    }


def seed_run_chain(conn, *, run_id, intent_id=None, considered_revision_id=None,
                   snapshot_id=None, scope_id=None, recognition_id=None, subject="u1",
                   considered_json=None):
    """`considered_json` defaults to `{}` — the shape every milestone/rail test needs, since none of
    them resolves an OPTION out of the revision. A test that does (the run→job trigger has to, to
    reach a real authoring strategy) passes the canonical v3 payload instead; the content hash stays
    `'cch'` either way because 1025's lineage trigger and `_seed_choice` both key on that literal."""
    intent_id = intent_id or f"{run_id}-intent"
    considered_revision_id = considered_revision_id or f"{run_id}-ccr"
    snapshot_id = snapshot_id or f"{run_id}-snap"
    scope_id = scope_id or f"{run_id}-scope"
    recognition_id = recognition_id or f"{run_id}-rec"
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES (%s, 'h', 'hypothesis') ON CONFLICT DO NOTHING", (intent_id,))
    conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
        "VALUES (%s, %s, %s, '{}') ON CONFLICT DO NOTHING",
        (run_id, intent_id, Jsonb({"subject": subject})))
    # input_content_hash is NOT decoration: 1024's contract_generation_input lineage trigger demands
    # a.input_content_hash = NEW.recognition_input_content_hash, and NULL = 'rh' is NULL, so a
    # recognition row without it makes the whole chain unseedable.
    conn.execute(
        "INSERT INTO intent_recognition_attempt (recognition_id, intent_id, input_hash, status, "
        "input_content_hash, taxonomy_version, applicability_mapping_version, recognizer_model_id, "
        "prompt_version, recipe_registry_version) "
        "VALUES (%s, %s, %s, 'resolved', 'rh', 'v1', 'v1', 'm', 'p1', 'r1') "
        "ON CONFLICT DO NOTHING", (recognition_id, intent_id, f"{run_id}-ih"))
    conn.execute(
        "INSERT INTO confirmed_generation_scope (scope_id, intent_id, generation_run_id, "
        "recognition_id, expansion, scope_mode, confirmation_source, confirmed_by) "
        "VALUES (%s, %s, %s, %s, 'none', 'scoped', 'user_confirmed', %s) ON CONFLICT DO NOTHING",
        (scope_id, intent_id, run_id, recognition_id, subject))
    conn.execute(
        "INSERT INTO contract_generation_input (generation_run_id, intent_id, recognition_id, "
        "confirmed_scope_id, redacted_hypothesis, recognition_input_content_hash, "
        "generation_input_content_hash, created_by) "
        "VALUES (%s, %s, %s, %s, 'h', 'rh', 'gh', %s) ON CONFLICT DO NOTHING",
        (run_id, intent_id, recognition_id, scope_id, Jsonb({"subject": subject})))
    conn.execute(
        "INSERT INTO catalog_metadata_snapshot (snapshot_id, generation_run_id, read_scope_hash, "
        "isolation_level, content_hash) VALUES (%s, %s, 'rs', 'repeatable read', 'ch') "
        "ON CONFLICT DO NOTHING", (snapshot_id, run_id))
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, metadata_snapshot_content_hash, "
        "considered_json, considered_content_hash, canonicalization_version) "
        "VALUES (%s, %s, %s, %s, 'ch', %s, 'cch', 'v1') ON CONFLICT DO NOTHING",
        (considered_revision_id, intent_id, run_id, snapshot_id,
         Jsonb(considered_json if considered_json is not None else {})))
    return {"run_id": run_id, "intent_id": intent_id,
            "considered_revision_id": considered_revision_id, "snapshot_id": snapshot_id,
            "scope_id": scope_id, "recognition_id": recognition_id, "subject": subject}
