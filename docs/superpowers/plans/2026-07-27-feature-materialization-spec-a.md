# Spec A — Executable Materialization Vertical Slice: Implementation Plan (rev 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A published `sandbox_feature.<group>` partition on the real Hadoop/Hive cluster, computed by generated Kedro/PySpark from governed formulas, with the numbers proven by execution.

**Done means a live table.** Task 17 is the deliverable.

**Spec:** `.../specs/2026-07-27-feature-materialization-spec-a-design.md` **rev 4**
**Verified interfaces:** `docs/architecture/2026-07-27-verified-interfaces-materialization.md`

> **THE RULE.** Plan revisions 1–3 were rejected with 12, 16 and 10 findings; **every defect sat in an API described from memory rather than read**. Before coding against any interface, confirm it in the verified-interfaces reference. If absent: **read the source, add an entry, then implement.**

> **Rev 4 restructures rev 3** around spec rev 4: physical inputs split by phase (T5 static / T15 run-time), L1 moves to run preparation, the capability probe becomes executable (T16), failure codes come from three closed enums (T1), and `FormulaPlannerIntentV1` is gone.

## Global Constraints

- Frozen slotted dataclasses + `StrEnum`. NOT pydantic.
- One hasher: `materialize_hash()` (T1). **Identity fields only** — no provenance, no timestamps, no run-time observations.
- Reuse governed machinery: `classify_join_path` · `graph_node` + `safety_floor.SENSITIVITY_ORDER` + `read_scope` · `read_operational_value` · `IdentityEnvelope`.
- **Never mint identity** (`authenticated=True` is a trust-root violation).
- **Render-only.** No `pyspark` import in `src/featuregen/materialize/`.
- **Every refusal uses a code from T1's closed enums.** A governed refusal is never a bare `TypeError`/`ValueError`.
- Manifests/findings carry counts, types, hashes, locations — **never data values**.
- Sandbox only · no fan-out repair · no scan sharing · `INSERT OVERWRITE` forbidden.
- Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

**Test command:** `PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize -p no:cacheprovider -q`
**Regression sweep (T3 touches shared code):** `pytest tests/featuregen/formula tests/featuregen/db tests/featuregen/overlay/upload -q`

---

### Task 0: `ClusterInventoryV1` — typed inventory, loader, metastore adapter

Spec §0. **Blocks T17 only**; T1–T16 proceed in parallel. The Markdown write-up is the human record; **compilation and run preparation consume the typed object**.

**Files:** Create `src/featuregen/materialize/inventory.py`, `conf/environments/hdfc-local-inventory.yml` (the typed artefact the loader reads), `docs/architecture/2026-07-27-hdfc-cluster-inventory.md` (the human record); Test `test_inventory.py`

**Produces:** `PartitionMappingV1` (closed variants per spec §3.4); `TableLayout` (frozen: `schema`, `table`, `partition_columns: tuple[tuple[str,str],...] | None`, `partition_mapping: PartitionMappingV1 | None`, `location`, `rewritten_in_place: bool`, `layout_fingerprint: str`); `EngineVersions` (frozen: `hive`, `spark`, `metastore`, `python`, `java`, `pyspark`, `kedro`); `ClusterInventoryV1` (frozen: `environment_id`, `tables`, `engine_versions`, `captured_at`); `load_inventory(path) -> ClusterInventoryV1`; `MetastoreInventoryAdapter.capture(conn, tables) -> ClusterInventoryV1`.

- [ ] **Step 1: Failing tests**

```python
def test_unpartitioned_is_explicit_not_absent(inventory_yaml_unpartitioned):
    inv = load_inventory(inventory_yaml_unpartitioned)
    assert inv.tables["banking.customers"].partition_columns is None   # VERIFIED unpartitioned


def test_a_table_missing_from_the_inventory_is_not_None(inventory_yaml_partial):
    inv = load_inventory(inventory_yaml_partial)
    with pytest.raises(KeyError):
        inv.tables["banking.transactions"]       # absent != unpartitioned


def test_every_runtime_version_is_required(inventory_yaml_missing_kedro):
    with pytest.raises(ValueError, match="kedro"):
        load_inventory(inventory_yaml_missing_kedro)


def test_adapter_captures_partition_columns_in_order(fake_metastore):
    inv = MetastoreInventoryAdapter().capture(fake_metastore, ["banking.transactions"])
    assert inv.tables["banking.transactions"].partition_columns == (("load_dt", "string"),)
```

- [ ] **Step 2–4:** Run/implement/run.
- [ ] **Step 5: Capture the real inventory** — if the cluster is reachable run the adapter; otherwise transcribe `DESCRIBE FORMATTED` / `SHOW PARTITIONS` for `banking.{transactions,accounts,customers}` into the YAML by hand. Record per table: partitioned or **verified unpartitioned**, ordered partition columns + types, example values covering the acceptance date, location, whether history is rewritten in place. Record the full `EngineVersions`.
- [ ] **Step 6: Answer the two slice-shaping questions** in the Markdown record:
  1. **Account-to-customer ownership.** A single `accounts.cif_id` is `N:1` and fine; a joint-holder bridge is `1:N` and **refuses `total_debit_amount_30d`** (spec §3.2) — record the substitute feature if so.
  2. **How a customer snapshot maps to a business date** → becomes the `SnapshotPolicy` variant (T4).
  3. **The logical→physical schema mapping per table.** Refs are schema-flattened to `public`; §3.5 needs this whenever `graph_node.schema_name` is NULL, or resolution refuses with `PHYSICAL_SCHEMA_NOT_RESOLVED`.
  4. **How each table's partitions map to a time window** → its `PartitionMappingV1`. A `load_dt` column does **not** imply an event-time mapping: late arrivals sit in load partitions outside the event range, so an `AVAILABILITY_PARTITION` mapping must widen the set. **Declare it; never infer it** — no mapping ⇒ `PARTITION_MAPPING_NOT_DECLARED`.
- [ ] **Step 7: Commit** — `feat(materialize): typed cluster inventory + metastore adapter`

---

### Task 1: `materialize_hash` + the four closed failure enums

Spec §14. Built first so every later task refuses with a listed code.

**Files:** Create `src/featuregen/materialize/{__init__,canonical,codes}.py`; Test `test_canonical.py`, `test_codes.py`

**Produces:** `materialize_hash(payload) -> str`; `CompilationRefusalCode`; `PublicationRefusalCode`; `ValidationGateCode`; `ValidationFindingCode`; `MaterializationRefused(Exception)` carrying `.code: CompilationRefusalCode | PublicationRefusalCode`.

- [ ] **Step 1: Failing tests**

```python
# test_canonical.py
def test_key_order_irrelevant():
    assert materialize_hash({"a": 1, "b": 2}) == materialize_hash({"b": 2, "a": 1})

def test_sha256_hex():
    h = materialize_hash({"a": 1})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)

def test_values_distinguished():
    assert materialize_hash({"a": 1}) != materialize_hash({"a": 2})

def test_rejects_non_mapping():
    with pytest.raises(TypeError):
        materialize_hash([1])  # type: ignore[arg-type]
```

```python
# test_codes.py — EXACT equality: `>=` would permit arbitrary extra codes and
# therefore would not test a CLOSED vocabulary at all.
def test_compilation_codes_are_exactly_the_spec_set():
    assert {c.value for c in CompilationRefusalCode} == {
        "AUTHORING_RUN_INCOMPLETE", "TERMINAL_PAYLOAD_TAMPERED", "NOT_RESOLVED",
        "FORMULA_HASH_MISMATCH", "AXES_MISMATCH", "INTENT_HASH_MISMATCH",
        "READ_SCOPE_INSUFFICIENT", "PROHIBITED_INPUT", "AMBIGUOUS_TABLE_NAME",
        "JOIN_PATH_NOT_VERIFIED", "JOIN_PATH_DENIED_BY_READ_SCOPE",
        "GRAIN_PATH_NOT_GOVERNED", "JOIN_FANOUT_UNSUPPORTED", "JOIN_CARDINALITY_UNKNOWN",
        "SPINE_SOURCE_NOT_DECLARED", "SPINE_DECLARATION_REJECTED_BY_FACTS",
        "PARTITION_MAPPING_NOT_DECLARED", "PHYSICAL_SCHEMA_NOT_RESOLVED",
        "AVAILABILITY_TIME_NOT_GOVERNED",
        "PHYSICAL_TYPE_UNSUPPORTED", "MULTIPLE_MATERIALIZATION_CONTRACTS",
        "PARTITION_IDENTITY_UNKNOWN", "UNACCOUNTED_LOGICAL_REF"}


def test_publication_codes_are_exactly_the_spec_set():
    """CAPABILITY_UNPROVEN / GROUP_BINDING_CONFLICT are PUBLICATION decisions —
    they are not compilation refusals and not runtime gates."""
    assert {c.value for c in PublicationRefusalCode} == {
        "CAPABILITY_UNPROVEN", "GROUP_BINDING_CONFLICT", "PUBLISH_MECHANISM_UNSUPPORTED"}


def test_gate_codes_are_exactly_the_spec_set():
    assert {c.value for c in ValidationGateCode} == {
        "KEY_NOT_UNIQUE", "MISSING_FEATURE_COLUMN", "UNEXPECTED_COLUMN", "WRONG_COLUMN_TYPE",
        "WRONG_NULLABILITY", "SCHEMA_HASH_MISMATCH", "MISSING_STAGING_MANIFEST",
        "STALE_STAGING_MANIFEST", "DUPLICATE_STAGING_MANIFEST", "IR_HASH_MISMATCH",
        "INCOMPLETE_COMPUTATION", "FORBIDDEN_NUMERIC", "OVERFLOW_VIOLATION",
        "SPINE_INCOMPLETE", "SPINE_DUPLICATE_KEY", "SPINE_NON_DETERMINISTIC",
        "RUN_PARAMETERS_MISSING", "PROJECT_INTEGRITY"}


def test_finding_codes_are_exactly_the_spec_set():
    assert {c.value for c in ValidationFindingCode} == {
        "PROJECT_DOES_NOT_BUILD", "PROJECT_HASH_MISMATCH", "PIPELINE_NOT_CONSTRUCTIBLE",
        "COLUMN_ABSENT", "COLUMN_TYPE_MISMATCH", "PARTITION_ABSENT", "READ_DENIED",
        "UNKNOWN_FINDING"}


def test_the_four_enums_do_not_overlap():
    """A code must belong to exactly one vocabulary, or a refusal cannot be typed."""
    sets = [{c.value for c in e} for e in
            (CompilationRefusalCode, PublicationRefusalCode, ValidationGateCode,
             ValidationFindingCode)]
    for i, a in enumerate(sets):
        for b in sets[i + 1:]:
            assert not (a & b), f"code appears in two enums: {a & b}"


def test_refusal_carries_a_typed_code_not_a_string():
    e = MaterializationRefused(CompilationRefusalCode.NOT_RESOLVED, "detail")
    assert e.code is CompilationRefusalCode.NOT_RESOLVED
    assert not isinstance(e.code, str) or isinstance(e.code, CompilationRefusalCode)


def test_spine_non_determinism_is_a_RUNTIME_gate_not_a_compilation_refusal():
    """An unresolved tie depends on actual rows, so it is discovered during execution."""
    assert "SPINE_NON_DETERMINISTIC" not in {c.value for c in CompilationRefusalCode}
    assert "SPINE_NON_DETERMINISTIC" in {c.value for c in ValidationGateCode}
```

- [ ] **Step 2: Run — FAIL** (`ModuleNotFoundError`)
- [ ] **Step 3: Implement** `canonical.py` (JCS + sha256 over a mapping) and `codes.py` (the four `StrEnum`s + `MaterializationRefused`).
- [ ] **Step 4: Run — PASS (11)**
- [ ] **Step 5: Commit** — `feat(materialize): hasher + four closed failure-code enums`

### Task 2: Terminal-event reader + Gate 1

Spec §1.2. `trace.py` has **no public event reader** — this task adds one.

**Files:** Modify `src/featuregen/formula/trace.py`; Create `src/featuregen/materialize/admission.py`, `tests/featuregen/materialize/fixtures.py`; Test `tests/featuregen/formula/test_trace_reader.py`, `test_admission.py`

**Produces:** `TerminalEvent` (frozen: `kind`, `payload`, `payload_hash`); `read_terminal_event(conn, run_id) -> TerminalEvent | None`; `ResolvedFeatureInput` (frozen: `intent`, `result`); `AdmittedFeature` (frozen: `feature_name`, `formula`, `formula_content_hash`, `intent`, `authoring_run_id`); `admit_artifacts(conn, inputs) -> tuple[AdmittedFeature, ...]`.

- [ ] **Step 1: Failing reader tests** — no terminal ⇒ `None`; terminal returns `kind`, `payload`, 64-char `payload_hash`.
- [ ] **Step 2–4:** Run/implement `read_terminal_event` via the existing private `_durable_read` (`trace.py:432`) so it inherits `run_status`'s visibility semantics/run.

- [ ] **Step 5: Hand-author fixtures with VERIFIED field names and VERIFIED output policies**

```python
# tests/featuregen/materialize/fixtures.py  (verified: interfaces reference §6, §7)
# TypedLiteral(type=…) · FilterPredicate has kind init=False and NO right_ref ·
# FilterBool uses children · WindowPolicy.unit is a WindowUnit ·
# Child-1 resolves plain SUM -> NON_ADDITIVE (no path_additive proof) and
# COUNT_DISTINCT -> NON_ADDITIVE with logical type `integer`.
# A fixture claiming otherwise is a FORGERY and Gate 1 rejects it.
```

Write `total_debit_amount_30d` (SUM + filter), `distinct_merchant_count_90d` (COUNT_DISTINCT, `output_type="integer"`), `cross_border_value_ratio_90d` (RATIO, `ZeroDenominator.NULL`), and `intent_for(name)`. **Run a construction check before continuing**; if a field name is wrong, fix the fixture *and* correct the interfaces reference.

- [ ] **Step 6: Failing Gate-1 tests**

```python
def test_no_terminal_event_is_refused(db, run_without_terminal, result_for):
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("f"), result_for(run_without_terminal))])
    assert e.value.code is CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE


def test_a_REJECTED_run_also_writes_COMPLETED_so_check_the_PAYLOAD(db, rejected_run, result_for):
    """VERIFIED: _TERMINAL_FOR_DISPOSITION maps only TECHNICAL_FAILURE to FAILED."""
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("f"), result_for(rejected_run))])
    assert e.value.code is CompilationRefusalCode.NOT_RESOLVED


def test_forged_result_citing_a_real_run_is_refused(db, resolved_run, forged_result):
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("f"), forged_result(resolved_run))])
    assert e.value.code is CompilationRefusalCode.FORMULA_HASH_MISMATCH


def test_axes_disagreeing_with_the_terminal_event_are_refused(db, resolved_run, tweaked_axes):
    ...  # AXES_MISMATCH

def test_tampered_terminal_payload_is_refused(db, tampered_terminal, result_for):
    ...  # TERMINAL_PAYLOAD_TAMPERED

def test_intent_hash_mismatch_is_refused(db, resolved_run, result_for):
    ...  # INTENT_HASH_MISMATCH

def test_feature_name_comes_from_the_intent(db, resolved_run, result_for):
    out = admit_artifacts(db, [ResolvedFeatureInput(intent_for("total_debit_amount_30d"),
                                                    result_for(resolved_run))])
    assert out[0].feature_name == "total_debit_amount_30d"


def test_no_function_accepts_a_bare_formula():
    import inspect, featuregen.materialize.admission as m
    for name, fn in inspect.getmembers(m, inspect.isfunction):
        if not name.startswith("_"):
            p = inspect.signature(fn).parameters
            assert "formula" not in p and "formulas" not in p
```

- [ ] **Step 7–9:** Run/implement the six §1.2 checks in order/run/commit — `feat(materialize): Gate 1 against the immutable terminal event`

---

### Task 3: Planner authority extension + `JoinPlan` adapter

Spec §3.1–3.2. **Two halves in order.** Touches shared code — run the overlay/upload sweep.

**Files:** Modify `src/featuregen/overlay/upload/join_path.py`; Create `src/featuregen/materialize/joins.py`; Test `tests/featuregen/overlay/upload/test_join_path_authority.py`, `test_joins.py`

**Produces:** `JoinStep` gains `approved_join_fact_key`, `approved_join_status`, `authority`; `JoinPlan` (frozen: `steps`, `outcome_kind`, `roles_used`, `fans_out`); `plan_join(conn, *, catalog_source, from_identity: PhysicalIdentity, to_identity: PhysicalIdentity, roles) -> JoinPlan | MaterializationRefused`.

**⚠️ Takes RESOLVED physical identities, not logical refs.** Spec §3.5: refs are schema-flattened to `public`, so a physical schema cannot be parsed out of one. Resolution is T5's job; T3 stays pure and receives `PhysicalIdentity(catalog_source, schema, table)`. Define that small frozen type here (T5 imports it) so the ordering works.

- [ ] **Step 1: Failing planner-authority tests** — an OPERATIONAL path's steps carry `approved_join_fact_key`/`approved_join_status` (**verified**: `clearing.append((from_ref, to_ref, card))` drops them today, though the SQL selects them at `:105-106`); a file-declared edge reports `authority="operational"` with a `None` fact key; **reverse-edge provenance and inverted cardinality both survive**.
- [ ] **Step 2–4:** Run/carry the two columns through the clearing tuple **inside the existing query** (no second read)/run. **Run the overlay/upload sweep** — this is shared code.

- [ ] **Step 5: Failing adapter tests**

```python
def test_bare_table_names_are_passed_to_the_planner(db, verified_join_catalog):
    """VERIFIED: _table_of returns parts[1]; a schema-qualified destination never matches."""
    r = plan_join(db, catalog_source="hdfc", from_identity=TXN, to_identity=ACCOUNTS, roles=("feature_engineer",))
    assert isinstance(r, JoinPlan) and r.steps


def test_unknown_cardinality_is_REFUSED(db, null_cardinality_catalog):
    """VERIFIED: JoinStep.cardinality is str|None and graph_edge.cardinality is NULLable.
    An unknown edge may BE 1:N — refusing only 1:N would be a fail-open."""
    r = plan_join(db, catalog_source="hdfc", from_identity=TXN, to_identity=ACCOUNTS, roles=("feature_engineer",))
    assert r.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_two_resolved_schemas_sharing_a_table_name_are_refused(db, ambiguous_intermediate_catalog):
    """The BFS indexes by BARE table name. Refs cannot express the ambiguity (all flattened
    to `public`, spec §3.5), so the check runs on RESOLVED physical identities: two distinct
    physical schemas containing the same table name anywhere on the path."""
    r = plan_join(db, catalog_source="hdfc", from_identity=TXN, to_identity=CUSTOMERS, roles=("feature_engineer",))
    assert r.code is CompilationRefusalCode.AMBIGUOUS_TABLE_NAME


def test_physical_continuity_is_validated_on_EVERY_step_not_just_endpoints(db, mixed_schema_path_catalog):
    r = plan_join(db, catalog_source="hdfc", from_identity=TXN, to_identity=CUSTOMERS, roles=("feature_engineer",))
    if isinstance(r, JoinPlan):
        schemas = {s.from_ref.split(".")[0] for s in r.steps} | \
                  {s.to_ref.split(".")[0] for s in r.steps}
        assert schemas == {"banking"}


def test_fan_out_toward_the_grain_is_REFUSED(db, joint_account_catalog):
    r = plan_join(db, catalog_source="hdfc", from_identity=TXN, to_identity=CUSTOMERS, roles=("feature_engineer",))
    assert r.code is CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED


def test_no_deduplication_helper_exists_in_the_module():
    import inspect
    from featuregen.materialize import joins
    src = inspect.getsource(joins)
    for banned in ("dropDuplicates", "drop_duplicates", "distinct("):
        assert banned not in src, "fan-out is refused, never repaired"


def test_authority_survives_into_the_plan(db, verified_join_catalog):
    r = plan_join(db, ...)
    assert r.steps[0].approved_join_fact_key is not None


def test_unverified_and_denied_map_to_distinct_codes(db, unverified_cat, restricted_cat, empty_cat):
    ...  # JOIN_PATH_NOT_VERIFIED / JOIN_PATH_DENIED_BY_READ_SCOPE / GRAIN_PATH_NOT_GOVERNED
```

- [ ] **Step 6–8:** Run/implement (parse with `parse_ref`, pass bare names, validate continuity on every step, refuse `None` cardinality and fan-out; **no BFS, no bridge scan, no prefix matching**)/run + overlay sweep/commit — `feat(materialize): JoinPlan adapter; planner retains authority`

---

### Task 4: Spine declaration + `SnapshotPolicy`

Spec §4, §4.2.

**Files:** Create `src/featuregen/materialize/spine.py`; Test `test_spine.py`

**Produces:** `PopulationSemantics`; `SnapshotPolicyKind` + the four variants; `SpineSourceDeclarationV1` with `identity_payload()` / `provenance_payload()`; `SpineSpec`; `validate_spine_declaration(conn, decl, *, roles) -> SpineSpec | MaterializationRefused`.

- [x] **Step 1: Failing tests**

```python
def test_facts_validate_but_never_choose(db, two_candidate_customer_tables, decl_for_customers):
    """kyc_customers ALSO has a unique cif_id; only the DECLARATION picks the master."""
    assert validate_spine_declaration(db, decl_for_customers,
                                      roles=("feature_engineer",)).source_table_ref \
           == "hdfc::banking.customers"


def test_facts_may_REJECT_a_declaration(db, decl_naming_non_unique_table):
    r = validate_spine_declaration(db, decl_naming_non_unique_table, roles=("feature_engineer",))
    assert r.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_missing_declaration_uses_a_GOVERNED_code_not_TypeError(db):
    r = validate_spine_declaration(db, None, roles=("feature_engineer",))
    assert r.code is CompilationRefusalCode.SPINE_SOURCE_NOT_DECLARED


def test_provenance_is_EXCLUDED_from_identity(decl_a, decl_b_same_semantics_different_declarer):
    """Two people making the same semantic declaration must produce the SAME contract."""
    assert decl_a.identity_payload() == decl_b_same_semantics_different_declarer.identity_payload()
    assert decl_a.provenance_payload() != decl_b_same_semantics_different_declarer.provenance_payload()
    for banned in ("declared_by", "declaration_reason", "recorded_at"):
        assert banned not in decl_a.identity_payload()


def test_population_semantics_is_closed():
    assert {p.value for p in PopulationSemantics} == {
        "current_complete_population", "current_active_only", "historical_as_of"}


def test_snapshot_policy_variants_are_closed():
    assert {k.value for k in SnapshotPolicyKind} == {
        "current_snapshot", "latest_available_as_of", "partition_mapped", "active_population"}


def test_current_active_only_REQUIRES_an_ActivePopulation_policy(db, decl_active_without_status):
    r = validate_spine_declaration(db, decl_active_without_status, roles=("feature_engineer",))
    assert r.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_no_free_text_sql_field_exists():
    import dataclasses
    from featuregen.materialize.spine import SpineSourceDeclarationV1
    names = {f.name for f in dataclasses.fields(SpineSourceDeclarationV1)}
    assert not any(("sql" in n) or ("predicate" in n) or ("where" in n) for n in names)


def test_declared_by_is_never_minted_in_this_module():
    import inspect
    from featuregen.materialize import spine
    assert "authenticated=True" not in inspect.getsource(spine)
```

- [x] **Step 2–5:** Run/implement/run/commit — `feat(materialize): spine declaration + closed SnapshotPolicy`

---

### Task 5: `PhysicalInputRequirement` — the STATIC half

Spec §3.3. Generation-time only. **No `business_dt` anywhere in this task.**

**Files:** Create `src/featuregen/materialize/inputs.py`; Test `test_inputs.py`

**Produces:** `PartitionMapping`; `PhysicalInputRequirement`; `resolve_physical_identity(conn, inventory, *, logical_ref) -> PhysicalIdentity | MaterializationRefused` (reuses the `column_authority.logical_ref_of` pattern — reads `graph_node.schema_name`, then the inventory's declared logical→physical mapping when it is NULL, else `PHYSICAL_SCHEMA_NOT_RESOLVED`); `derive_requirement(conn, inventory, *, logical_ref) -> PhysicalInputRequirement | MaterializationRefused`.

- [ ] **Step 1: Failing tests**

```python
def test_requirement_takes_no_business_dt():
    import inspect
    from featuregen.materialize.inputs import derive_requirement
    assert "business_dt" not in inspect.signature(derive_requirement).parameters


def test_physical_schema_is_RESOLVED_never_parsed_from_the_ref(db, seeded_catalog, inventory):
    """Refs are schema-flattened to `public`; the real schema lives in graph_node.schema_name."""
    ident = resolve_physical_identity(db, inventory, logical_ref="hdfc::public.transactions.amount")
    assert ident.schema == "banking"          # from schema_name, NOT from the ref segment


def test_null_schema_name_falls_back_to_the_DECLARED_mapping(db, no_schema_name, inventory):
    assert resolve_physical_identity(db, inventory,
                                     logical_ref="hdfc::public.transactions.amount").schema == "banking"


def test_unresolvable_schema_REFUSES_rather_than_defaulting_to_public(db, no_schema_name, bare_inventory):
    """Silently defaulting to `public` would read a DIFFERENT table than the catalog governs."""
    r = resolve_physical_identity(db, bare_inventory, logical_ref="hdfc::public.transactions.amount")
    assert r.code is CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED


def test_mapping_comes_from_the_DECLARATION_not_the_column_name(inv_load_dt_no_mapping):
    r = derive_requirement(inv_load_dt_no_mapping, table_ref="hdfc::banking.transactions")
    assert r.code is CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED


def test_recapturing_identical_metadata_keeps_the_same_identity(inv_a, inv_b_same_layout_later):
    """captured_at must NOT reach identity, or a rescan changes the generated project."""
    assert derive_requirement(inv_a, table_ref="hdfc::banking.transactions") == \
           derive_requirement(inv_b_same_layout_later, table_ref="hdfc::banking.transactions")


def test_changing_layout_or_a_physical_type_changes_the_fingerprint(inv_a, inv_relayout):
    assert derive_requirement(inv_a, table_ref="hdfc::banking.transactions").layout_fingerprint != \
           derive_requirement(inv_relayout, table_ref="hdfc::banking.transactions").layout_fingerprint


def test_requirement_records_partition_COLUMNS_not_values(partitioned_inventory):
    req = derive_requirement(partitioned_inventory, table_ref="hdfc::banking.transactions")
    assert req.partition_columns == (("load_dt", "string"),)
    assert not hasattr(req, "partition_specs")     # values are a RUN-time concern


def test_verified_unpartitioned_is_None(unpartitioned_inventory):
    assert derive_requirement(unpartitioned_inventory,
                              table_ref="hdfc::banking.customers").partition_columns is None


def test_a_table_absent_from_the_inventory_REFUSES(empty_inventory):
    r = derive_requirement(empty_inventory, table_ref="hdfc::banking.transactions")
    assert r.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN


def test_requirement_is_stable_across_business_dates(partitioned_inventory):
    """The whole point: the generated project must not change every day."""
    a = derive_requirement(partitioned_inventory, table_ref="hdfc::banking.transactions")
    b = derive_requirement(partitioned_inventory, table_ref="hdfc::banking.transactions")
    assert a == b
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): static PhysicalInputRequirement`

---

### Task 6: `ExpressionExecutionIR` + reference completeness

Spec §2.1, §3.

**Files:** Create `src/featuregen/materialize/expression_ir.py`; Test `test_expression_ir.py`

**Produces:** `PhysicalRef`; `PitSpec`; `ExpressionExecutionIR`; `compile_expression(conn, *, expr_path, expr, grain_keys, roles, inventory) -> ExpressionExecutionIR | MaterializationRefused`; `expression_ir_hash(e)`.

- [ ] **Step 1: Failing tests** — read set contains operand + filter `left` + event-time + every join endpoint (**there is no `right_ref`**) · a ratio's two expressions get **independent** `PitSpec`s · different windows hash differently · missing `AVAILABILITY_TIME` ⇒ `AVAILABILITY_TIME_NOT_GOVERNED` · a fan-out or unknown-cardinality join refuses the expression · the IR field is named **`input_requirements: tuple[PhysicalInputRequirement, ...]`** (asserted by name; `input_snapshots` must NOT exist on the IR) · `identity_payload()` excludes provenance **and any partition value** · **reference completeness**: every `logical_ref` from the expression appears in the read set or is explicitly classified non-physical, else `UNACCOUNTED_LOGICAL_REF`.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): per-expression IR + reference completeness`

---

### Task 7: `FormulaExecutionIRV1` + group-wide Gate 2

Spec §1.3, §3.

**Files:** Create `src/featuregen/materialize/ir.py`; Test `test_ir.py`

**Produces:** `FormulaExecutionIRV1`; `compile_ir(conn, admitted, *, roles, spine_decl, inventory)`; `ir_hash(ir)`; `AuthorizedCompilation`; `authorize_compilation(conn, irs, spine, *, roles) -> AuthorizedCompilation | MaterializationRefused`.

- [ ] **Step 1: Failing tests**

```python
def test_gate_2_is_group_wide_not_per_feature(db, public_ir, denied_ir, spine):
    """A public feature IS individually authorized; the GROUP operation must fail."""
    assert authorize_compilation(db, (public_ir,), spine, roles=("feature_engineer",))
    r = authorize_compilation(db, (public_ir, denied_ir), spine, roles=("feature_engineer",))
    assert r.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT


def test_refusal_produces_no_contract_plan_or_project(db, public_ir, denied_ir, spine, spy):
    authorize_compilation(db, (public_ir, denied_ir), spine, roles=("feature_engineer",))
    assert spy.contracts_derived == 0 and spy.projects_rendered == 0


def test_gate_2_covers_join_endpoints_the_spine_and_availability_columns(db, ...):
    ...  # three cases, each READ_SCOPE_INSUFFICIENT


def test_ratio_produces_two_expression_irs(db, seeded, admitted_ratio, spine):
    assert {e.expr_path for e in compile_ir(...).expressions} == {"body.numerator", "body.denominator"}


def test_output_policy_is_carried_never_rederived(db, seeded, admitted, spine):
    assert compile_ir(...).output_policy == admitted.formula.output


def test_ir_hash_excludes_run_time_values(db, seeded, admitted, spine):
    payload = compile_ir(...).identity_payload()
    for banned in ("business_dt", "partition_specs", "input_snapshot_ids", "resolved_at"):
        assert banned not in str(payload)
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): FormulaExecutionIRV1 + group-wide Gate 2`

---

### Task 8: Physical type adapter

Spec §6.

**Files:** Create `src/featuregen/materialize/physical_types.py`; Test `test_physical_types.py`

**Produces:** `PHYSICAL_TYPE_POLICY_VERSION = 1`; `PhysicalType` (frozen: `sql_type`, `nullable`, **plus `rounding` / `overflow`** — §6 requires both to be explicit in generated code, and a renderer cannot honour what it never receives); `resolve_physical_type(formula) -> PhysicalType | MaterializationRefused`.

- [x] **Step 1: Failing tests** — counts → `BIGINT` · SUM/RATIO/DIFFERENCE → `DECIMAL(p,s)` from `DecimalPolicy` · **operation beats the logical word** (`COUNT_DISTINCT` is logically `integer`, physically `BIGINT`) · `ZeroDenominator.NULL` ⇒ nullable · `EmptyWindowResult.ZERO` ⇒ non-nullable · precision > 38 ⇒ `PHYSICAL_TYPE_UNSUPPORTED` · `SATURATE` ⇒ refused · **`DOUBLE` never appears in the module source**.
- [x] **Step 2–5:** Run/implement/run/commit — `feat(materialize): versioned physical type adapter`

**Established (interfaces reference §24), beyond the sketch above:** the logical word is read as **operand evidence** and an unreadable/inexact operand refuses · `NullInput.PROPAGATE` is a **third** nullability source §6 does not list · a count's `DecimalPolicy` governs nothing and is neither validated nor carried · the body-shape gate is `schema.body_expressions`, not a second local check. Six gaps recorded in `DEFERRED-WORK.md` A.8 — the load-bearing one is that a **ratio's operands and a difference's subtrahend are invisible** to the operand check from the formula alone.

---

### Task 8.1: Exact-numeric operand evidence (BLOCKS Task 10)

Spec §6. Architect's ruling, 2026-07-27: a known-unsupported numeric representation must be refused **before the group plan authorizes generated execution** — not merely "before leaving sandbox".

**Why this exists:** `_resolve_ratio` documents "numeric both operands" and never checks it, and the obvious fix is wrong — `_is_numeric_logical_type` accepts `float`, `double`, `double precision`, `real`, `money`, so a float ratio passes it and still publishes fixed-point. *Numeric* and *exact-numeric* are different questions.

**Files:** Modify `src/featuregen/materialize/expression_ir.py`, `physical_types.py`; Test `test_expression_ir.py`, `test_physical_types.py`

**Produces:** `ExpressionExecutionIR` gains **`operand_type` evidence** (the governed C1 type of that expression's operand, or an explicit "unavailable" marker); `resolve_physical_type` validates **every arithmetic operand**; `PHYSICAL_TYPE_POLICY_VERSION` **increments**.

**Scope note:** the sibling Child-1 fix (make `_resolve_ratio` enforce its documented rule, and distinguish *unavailable type authority* from *a governed non-numeric type*) is routed to the feature-generation owner. **Do not edit that file from this stream** — it is actively being worked.

- [ ] **Step 1: Failing tests** — the acceptance set is fixed by the ruling:

```python
def test_exact_decimal_ratio_survives(...): ...          # decimal/integer operands → DECIMAL(p,s)
def test_string_operand_dies(...): ...                   # → PHYSICAL_TYPE_UNSUPPORTED
def test_unknown_or_unreadable_type_dies_or_requires_authority(...): ...
                                                         # state WHICH is chosen and why
def test_float_numerator_dies(...): ...
def test_float_denominator_dies(...): ...
def test_float_difference_subtrahend_dies(...): ...
def test_money_operand_dies(...): ...
```

Plus the mutation harness's **must-survive no-op** control.

- [ ] **Step 2: Run — FAIL** · **Step 3: Implement** — carry the governed operand type per expression; gate `DECIMAL` production on the exact-numeric allowlist; increment the policy version · **Step 4: Run — PASS** · **Step 5: Commit** — `feat(materialize): exact-numeric operand evidence gates DECIMAL`

---

### Task 9: Classification + per-feature contracts + grouping

Spec §5.

**Files:** Create `src/featuregen/materialize/{classify,contract}.py`; Test `test_classify.py`, `test_contract.py`

**Produces:** `CLASSIFICATION_POLICY_VERSION = 1`; `RETENTION_POLICY_VERSION = 1`; `DEFAULT_RETENTION_CLASS`; `classify_read_set(conn, refs) -> Classification | MaterializationRefused`; `CadenceDecl`; `AvailabilityClass`; `ContractOverrides`; `MaterializationContractV1`; `derive_contract(...)`; `group_by_contract(contracts)`; `contract_hash(c)`.

- [ ] **Step 1: Failing classification tests**

```python
def test_sensitivity_class_comes_from_effective_restriction(db, confidential_catalog, refs):
    assert classify_read_set(db, refs).sensitivity_class == "confidential"


def test_access_requirements_come_from_the_read_scope_TAGS(db, pii_catalog, refs):
    assert "pii_reader" in classify_read_set(db, refs).access_requirements


def test_the_two_axes_are_independent(db, pii_but_internal_catalog, refs):
    c = classify_read_set(db, refs)
    assert c.sensitivity_class == "internal" and "pii_reader" in c.access_requirements


def test_unknown_restriction_normalizes_then_REFUSES(db, garbage_restriction_catalog, refs):
    """Normalize-then-refuse: unknown → prohibited internally → one public refusal."""
    r = classify_read_set(db, refs)
    assert r.code is CompilationRefusalCode.PROHIBITED_INPUT


def test_a_join_key_or_the_spine_can_be_the_most_restrictive(db, restricted_join_key, refs):
    assert classify_read_set(db, refs).sensitivity_class == "restricted"
```

- [ ] **Step 2: Failing contract tests** — contracts derived **per feature** · mixed contracts ⇒ `MULTIPLE_MATERIALIZATION_CONTRACTS` listing both groups, **not** a union · 30d and 90d share a contract · hash excludes calculation window · hash excludes live observations · hash includes **all three** policy versions (classification, physical-type, retention) **and the spine's `identity_payload()` only — never its provenance** · override may tighten, not loosen · `dependencies_ready` trigger refused · invalid timezone refused.
- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): classification + per-feature contracts + grouping`

---

### Task 10: Group plan, staging manifest, group binding

Spec §9, §10.1.

**Files:** Create `src/featuregen/materialize/{group_plan,binding}.py`; Test `test_group_plan.py`, `test_binding.py`

**Produces:** `PlannedFeature` (incl. resolved `PhysicalType`); `FeatureGroupPlanV1`; `StagingManifestV1`; `expected_schema(plan)`; `check_completeness(...)`; `GroupContractBinding`; `GroupPlanRevision`; `bind_group(...)` and `current_plan_revision(revisions)` as **pure functions** — their tables are created in T14, so persistence tests live there and T10 stays pure.

- [ ] **Step 1: Failing tests** — a matching schema with a wrong `ir_hash` still fails (`IR_HASH_MISMATCH`) · a manifest from a **different generation/run/business_dt** ⇒ `STALE_STAGING_MANIFEST` · duplicate manifest ⇒ `DUPLICATE_STAGING_MANIFEST` · missing/failed manifest · missing/extra/mistyped column · **nullability mismatch** ⇒ `WRONG_NULLABILITY` · `expected_schema` includes the three **system columns** (`__generation_id`, `__generated_project_hash`, `__sandbox_execution_hash`) · adding a feature changes `group_plan_hash` and **keeps** the binding · a **different contract hash for the same logical name** ⇒ `PublicationRefusalCode.GROUP_BINDING_CONFLICT` · **the binding record has no mutable field**: adding a feature appends a `GroupPlanRevision`, and `current_plan_revision` DERIVES the current plan from the latest successfully-published revision · name collision is a plan error.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): group plan, staging manifests, contract-bound target`

---

### Task 11: Two-phase identity

Spec §7.

**Files:** Create `src/featuregen/materialize/identity.py`; Test `test_identity.py`

**Produces:** `CompilationIdentity`; `RenderedArtifactIdentity`; `derive_namespace()`; `sandbox_execution_hash(...)`.

- [ ] **Step 1: Failing tests** — `derive_namespace()` takes **no parameters**, and the module source contains no production literal · hashes are **plural** · `RenderedArtifactIdentity` is built after rendering · **`generated_project_hash` appears in `GENERATED.lock` and in no other file**, and is computed over every file *except* the lock · `sandbox_execution_hash` includes `business_dt`, `input_snapshot_ids` and the capability attestation id, and is reproducible · `CompilationIdentity` contains **none** of those run-time values · no `production_execution_hash` exists.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): two-phase sandbox-only identity`

---

### Task 12: Render the complete runnable project

Spec §7.

**Files:** Create `src/featuregen/materialize/render/{__init__,project}.py`; Test `test_render_project.py` + `goldens/`

- [ ] **Step 1: Failing tests** — every required file emitted (`settings.py`, `pipeline_registry.py`, `pipelines/materialize/{nodes,pipeline}.py`, `conf/base/*`, `GENERATED.lock`, `README.md`, `pyproject.toml`, `requirements.lock`) · pipeline wires explicit `inputs=`/`outputs=` · settings registers both hooks · README states the `kedro run` vs `spark-submit` distinction · **pinned dependency versions come from `ClusterInventoryV1.engine_versions`** · catalog names only read-set + spine tables · target is `sandbox_feature.*` and **not** parameterized · every `.py` parses · deterministic · `materialize_to` writes a real directory.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): render a complete runnable Kedro project`

---

### Task 13: Render compute nodes

Spec §4.2, §8.

**Files:** Create `render/nodes_compute.py`; Test `test_render_compute.py`

- [ ] **Step 1: Failing tests** — spine reads the **declared** source, never a fact table · **each `SnapshotPolicy` variant renders its own selection**, and `LATEST_AVAILABLE_AS_OF` renders the effective-time filter, the availability filter and the deterministic tie-break · availability gate uses the governed column · `event_time_plus_lag` renders its lag · **calendar windows are not converted to days** · projection selects only read-set columns, never `*` · calculate writes a `StagingManifestV1` carrying `ir_hash`, generation, run and business_dt · **per-feature staging carries ONLY `(keys…, business_dt, one feature column)` — no system columns**, since duplicating them across staging outputs collides at assembly · rendered overflow behaviour **raises** rather than yielding NULL · rounding is explicit · every rendered node parses.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): render spine, PIT projection, per-feature calculation`

---

### Task 14: Render gates + hooks + control plane

Spec §9, §12.

**Files:** Create `render/nodes_gate.py`, `src/featuregen/materialize/control_plane.py`, `src/featuregen/db/migrations/1031_materialization_control_plane.sql`; Test `test_render_gate.py`, `test_control_plane.py`, `test_migration_1031.py`

- [ ] **Step 1: Failing migration tests** — `materialization_generation`, `pipeline_validation_report`, `materialization_run_event`, `materialization_run_manifest`, `publication_capability_attestation`, `group_binding` **all** reject UPDATE, DELETE **and TRUNCATE** (statement-level `BEFORE TRUNCATE … FOR EACH STATEMENT`; a `FOR EACH ROW` trigger does not fire on TRUNCATE) · `(run_id, seq)` unique on events · closed `event_kind` CHECK · manifest FKs to `generation_id` · one terminal manifest per run.
- [ ] **Step 2: Failing gate tests** — every `ValidationGateCode` rendered · assembly consumes staging manifests · **assembly adds each of the three system columns exactly once** (assert exactly one copy of each) · `__generated_project_hash` is **read from `GENERATED.lock` at runtime** and **no generated source file contains the project-hash literal** · `__sandbox_execution_hash` arrives via prepared run parameters · a failed gate raises · the manifest writer never calls `collect()`/`take()`/`head()` · `run_status` folds from events.
- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): rendered gates, hooks, append-only control plane`

---

### Task 15: Run preparation — snapshots, execution identity, validation loop

Spec §3.3, §11. **This is where `business_dt` first appears.**

**Files:** Create `src/featuregen/materialize/{runprep,validation,submit}.py`; Test `test_runprep.py`, `test_validation.py`, `test_submit.py`

**Produces:** `PhysicalInputSnapshot`; `RunInputRequest` (frozen: `feature_name`, `expr_path`, `physical_requirement`, `pit_spec`); `resolve_snapshots(inventory, metastore, *, requests: tuple[RunInputRequest, ...], business_dt) -> tuple[...] | MaterializationRefused` (keyed by `(feature_name, expr_path, requirement_id)`; identical reads de-duplicated only AFTER comparing semantics); `RunPreparation` (snapshots + `sandbox_execution_hash` + **`parameters`** for execution); `ValidationLevel`; `FindingClass`; `ValidationReportV1`; `run_l0`; `run_l1`; `classify`; `may_regenerate`; `LocalClusterSubmitter`.

- [ ] **Step 1: Failing run-prep tests**

```python
def test_each_expression_resolves_its_OWN_snapshots(daily_inventory, metastore, group_requests):
    """A 30d feature, two 90d features and a ratio's two expressions cannot share one window."""
    snaps = resolve_snapshots(daily_inventory, metastore, requests=group_requests,
                              business_dt="2026-07-27")
    assert {(s.feature_name, s.expr_path) for s in snaps} == {
        ("total_debit_amount_30d", "body.expr"),
        ("distinct_merchant_count_90d", "body.expr"),
        ("cross_border_value_ratio_90d", "body.numerator"),
        ("cross_border_value_ratio_90d", "body.denominator")}


def test_availability_mapping_widens_beyond_the_event_window(availability_inventory, metastore):
    """Late arrivals sit in load partitions OUTSIDE the event range."""
    snaps = resolve_snapshots(availability_inventory, metastore, requests=(req_90d,),
                              business_dt="2026-07-27")
    assert len(snaps[0].partition_specs) > 90


def test_a_DECLARED_one_day_event_mapping_resolves_90_partitions(event_inventory, metastore):
    """Valid ONLY for a declared one-day EVENT_TIME_PARTITION mapping — not a general rule."""
    snaps = resolve_snapshots(event_inventory, metastore, requests=(req_90d,),
                              business_dt="2026-07-27")
    assert len(snaps[0].partition_specs) == 90


def test_business_dt_is_never_assumed_to_BE_the_partition_column(load_dt_inventory, metastore):
    snaps = resolve_snapshots(load_dt_inventory, metastore, requirements=(req,),
                              business_dt="2026-07-27", window=_window(1))
    cols = {c for s in snaps[0].partition_specs for c, _ in s.columns}
    assert cols == {"load_dt"}


def test_snapshots_do_NOT_change_the_ir_hash(ir, daily_inventory, metastore):
    """The generated project must be identical across business dates."""
    before = ir_hash(ir)
    resolve_snapshots(daily_inventory, metastore, requirements=(req,),
                      business_dt="2026-07-27", window=_window(30))
    assert ir_hash(ir) == before


def test_execution_hash_DOES_change_with_business_dt(compilation_identity, snaps_a, snaps_b):
    assert sandbox_execution_hash(compilation_identity, business_dt="2026-07-27", ...) != \
           sandbox_execution_hash(compilation_identity, business_dt="2026-07-28", ...)


def test_verified_unpartitioned_yields_None(unpartitioned_inventory, metastore):
    assert resolve_snapshots(...)[0].partition_specs is None
```

- [ ] **Step 2: Failing validation tests** — **L0 imports the project and builds the Kedro pipeline** (a project `ast.parse` accepts but with no pipeline must FAIL as `PIPELINE_NOT_CONSTRUCTIBLE`) · L0 catches a hand-edited project · **L1 runs at run preparation over ALL IRs, all expressions and the spine, verifying every resolved partition exists** · L1 reads metadata only · type contradiction ⇒ `GOVERNED_FACT_MISMATCH` · missing partition ⇒ `ENVIRONMENT_OR_DATA` · unknown code ⇒ `UNCLASSIFIED` · both `GOVERNED_FACT_MISMATCH` and `UNCLASSIFIED` block regeneration · findings carry no data values · unreachable cluster ⇒ `status="error"` with zero findings · L2 not run unless requested.
- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): run preparation + validation loop + submitter`

---

### Task 16: The executable publication-capability probe

Spec §10.3. **An attestation can only exist by ingesting a probe result.**

**Files:** Create `src/featuregen/materialize/publish.py`, `render/publish.py`; Test `test_publish.py`, `test_probe.py`

**Produces:** `PublishMechanism`; `ProbeObservation`; `ProbeResult` (frozen: `observations`, `evidence_hash`, `passed`, `covers_schema_evolution`, `engine_versions`); `probe_publication_capability(cluster, *, mechanism, engine_versions) -> ProbeResult`; `PublicationCapabilityAttestation`; `record_attestation(conn, probe_result)`; `PublisherSelection`; `select_publisher(conn, *, environment_id, engine_versions, mechanism, group_plan, published_schema) -> PublisherSelection | MaterializationRefused`; `render_publish(plan, *, selection)`.

- [ ] **Step 1: Failing tests**

```python
def test_record_attestation_accepts_ONLY_a_probe_result():
    import inspect
    params = inspect.signature(record_attestation).parameters
    assert set(params) - {"conn"} == {"probe_result"}   # no passed=True back door


def test_no_attestation_means_no_publisher(db):
    r = select_publisher(db, environment_id="hdfc-local", ...)
    assert r.code is CompilationRefusalCode.CAPABILITY_UNPROVEN or r.code == "CAPABILITY_UNPROVEN"


def test_attestation_for_another_environment_does_not_count(db, attestation_other_env):
    ...  # refused, message names hdfc-local


def test_engine_version_drift_invalidates_the_attestation(db, attestation_spark_3_4, cluster_spark_3_5):
    ...  # refused


def test_adds_feature_is_DERIVED_not_passed():
    import inspect
    assert "adds_feature" not in inspect.signature(select_publisher).parameters
    # it is computed from published_schema vs group_plan


def test_adding_a_feature_needs_schema_evolution_coverage(db, attestation_without_schema_evolution,
                                                          plan_with_extra_feature, published_schema):
    r = select_publisher(db, ..., group_plan=plan_with_extra_feature,
                         published_schema=published_schema)
    assert r.code == "CAPABILITY_UNPROVEN"


def test_render_publish_consumes_a_SELECTION_not_a_mechanism():
    import inspect
    assert "selection" in inspect.signature(render_publish).parameters
    assert "mechanism" not in inspect.signature(render_publish).parameters


def test_no_insert_overwrite_anywhere(plan, selection):
    assert "INSERT OVERWRITE" not in render_publish(plan, selection=selection).upper()
```

- [ ] **Step 2: The probe itself** (run against the real cluster; drives the attestation)

```python
# tests/featuregen/materialize/test_probe.py
def test_probe_observes_only_complete_states(live_cluster):
    """Publish A, poll readers continuously, switch to B; every observation must be a
    COMPLETE A or COMPLETE B, discriminated by __generation_id plus a content check."""
    result = probe_publication_capability(live_cluster, mechanism=PublishMechanism.VERSIONED_POINTER,
                                          engine_versions=live_cluster.engine_versions)
    assert result.observations, "probe observed nothing — it would pass vacuously"
    assert result.passed


def test_probe_covers_ADDING_a_feature(live_cluster):
    """A partition-location swap does not atomically change table SCHEMA."""
    result = probe_publication_capability(live_cluster, ...)
    assert result.covers_schema_evolution


def test_attestation_carries_the_probe_evidence_hash(db, live_cluster):
    att = record_attestation(db, probe_publication_capability(live_cluster, ...))
    assert att.evidence_hash and att.passed
```

- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): executable capability probe + evidence-backed publisher`

---

### Task 17: LIVE — Spark-local proof, then a published cluster partition

Spec §13. **The deliverable.** Blocked on Task 0.

**Files:** Create `src/featuregen/materialize/pipeline.py`, `tests/featuregen/materialize/spark_fixtures/`; Test `test_spark_local.py`, `test_live_cluster.py`

- [ ] **Step 1: Hand-author the fixtures.** `transactions` (a row dated `2026-07-01` **posted** `2026-07-05`; three distinct merchants for customer 1001; cross-border and domestic amounts; a 1/3 ratio forcing rounding; a value overflowing the declared precision), `accounts`, `customers` (including customer `1099` with **no** transactions). Every expected value hand-computed, with its arithmetic in a comment. Add `pyspark` to dev dependencies.

- [ ] **Step 2: Failing Spark-local tests (MANDATORY, run by default) — explicit values for ALL THREE features**

```python
def test_total_debit_amount_30d(spark_run):
    assert spark_run(business_dt="2026-07-27").value("1001", "total_debit_amount_30d") \
           == Decimal("5500.00")                      # 3000 + 2000 + 500 debits


def test_distinct_merchant_count_90d(spark_run):
    assert spark_run(business_dt="2026-07-27").value("1001", "distinct_merchant_count_90d") == 3
                                                      # M1, M2, M3 (M1 appears twice)


def test_cross_border_value_ratio_90d(spark_run):
    assert spark_run(business_dt="2026-07-27").value("1001", "cross_border_value_ratio_90d") \
           == Decimal("0.20")                         # 1000 cross-border / 5000 total


def test_zero_denominator_yields_null_per_policy(spark_run):
    assert spark_run(business_dt="2026-07-27").value("1002", "cross_border_value_ratio_90d") is None


def test_half_up_rounding_at_scale_2(spark_run):
    assert spark_run(business_dt="2026-07-27").value("1003", "cross_border_value_ratio_90d") \
           == Decimal("0.33")                         # 1/3 -> HALF_UP at scale 2


def test_look_ahead_row_is_excluded(spark_run):
    """Dated 2026-07-01, posted 2026-07-05: invisible on the 3rd, visible on the 6th."""
    assert spark_run(business_dt="2026-07-03").value("1004", "total_debit_amount_30d") == 0
    assert spark_run(business_dt="2026-07-06").value("1004", "total_debit_amount_30d") == 250


def test_entity_with_no_transactions_still_appears(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.has_key("1099") and out.value("1099", "total_debit_amount_30d") == 0


def test_exactly_one_row_per_key_and_date(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.row_count() == out.distinct_key_count()


def test_future_customer_version_is_excluded_from_the_spine(spark_run_scd):
    assert not spark_run_scd(business_dt="2026-07-27").has_key("1200")   # effective 2026-08-01


def test_overflow_RAISES_rather_than_yielding_null(spark_run_overflow):
    """Spark's default is NULL on decimal overflow; OverflowBehavior.ERROR must fail."""
    with pytest.raises(Exception, match="OVERFLOW_VIOLATION|overflow"):
        spark_run_overflow(business_dt="2026-07-27")


def test_orphan_key_blocks_a_complete_population_claim(spark_run_orphan):
    with pytest.raises(Exception, match="SPINE_INCOMPLETE"):
        spark_run_orphan(business_dt="2026-07-27")


def test_duplicate_spine_key_is_rejected(spark_run_dup_spine):
    with pytest.raises(Exception, match="SPINE_DUPLICATE_KEY"):
        spark_run_dup_spine(business_dt="2026-07-27")


def test_stale_staging_manifest_blocks_publication(spark_run_stale):
    with pytest.raises(Exception, match="STALE_STAGING_MANIFEST"):
        spark_run_stale(business_dt="2026-07-27")
```

- [ ] **Step 3: Run — FAIL. Iterate: run → read the failure → fix the RENDERER (never the expected value) → re-run**, until every number is right.

- [ ] **Step 4: Failing live-cluster tests**

```python
def test_capability_is_proven_by_RUNNING_the_probe(live, control_plane):
    """Not a seeded attestation — the probe executes here."""
    result = probe_publication_capability(live, mechanism=PublishMechanism.VERSIONED_POINTER,
                                          engine_versions=live.engine_versions)
    assert result.passed and result.covers_schema_evolution and result.observations
    record_attestation(control_plane.conn, result)


def test_generate_validate_run_and_publish(live, control_plane, resolved_inputs):
    result = generate_group(control_plane.conn, resolved_inputs, ...)
    assert run_l0(result.project, workdir=live.workdir).status == "passed"
    prep = prepare_run(result, business_dt="2026-07-27", inventory=live.inventory,
                       metastore=live.metastore)
    for ir in result.irs:                                   # ALL IRs, not just the first
        assert run_l1(control_plane.conn, ir, prep, roles=(...)).status == "passed"
    live.submit_and_run(result.project, business_dt="2026-07-27")

    pub = live.describe("sandbox_feature.cif_daily")
    assert pub.schema == expected_schema(result.plan)        # includes the 3 system columns
    for f in result.plan.features:
        assert pub.non_null_count(f.column_name) > 0         # not a table of nulls
    assert pub.value_of("__generation_id") == result.generation_id
    assert pub.value_of("__generated_project_hash") == result.project.generated_project_hash
    assert pub.row_count == control_plane.latest_manifest().published_row_count
    assert control_plane.run_status(result.run_id) == "published"


def test_the_acceptance_fixture_gives_the_SAME_numbers_on_the_cluster(live, acceptance_fixture):
    out = live.run_acceptance(acceptance_fixture, business_dt="2026-07-27")
    assert out.value("1001", "total_debit_amount_30d") == Decimal("5500.00")
    assert out.value("1001", "distinct_merchant_count_90d") == 3
    assert out.value("1001", "cross_border_value_ratio_90d") == Decimal("0.20")


def test_reports_and_manifest_are_ingested(live, control_plane):
    assert control_plane.latest_manifest().generation_id
    assert control_plane.reports_for(level="L1")
```

- [ ] **Step 5: Full sweep, then commit**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize -p no:cacheprovider -q
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/formula tests/featuregen/db \
    tests/featuregen/overlay/upload -p no:cacheprovider -q
git add -- src/featuregen/materialize tests/featuregen/materialize pyproject.toml
git commit -m "feat(materialize): end-to-end generation, Spark-local proof, live publication"
```

**Spec A is not done until `test_live_cluster.py` passes against the real cluster.**

---

## Self-Review

**Rev-3 findings, each owned:** phase split → T5 (static) / T15 (run-time), with tests asserting `ir_hash` is unchanged by snapshots and `sandbox_execution_hash` changes with `business_dt` · executable probe → T16, `record_attestation` takes only a `ProbeResult` and `adds_feature` is not a parameter · `SnapshotPolicy` → T4 closed variants + T13 rendering + T17 SCD execution test · group binding → T10 · system columns → T10 `expected_schema` + T13 rendering + T17 assertions · group-wide Gate 2 → T7, with a test that a public IR *is* individually authorized · join fail-opens → T3 (`JOIN_CARDINALITY_UNKNOWN`, ambiguous *intermediate*) · closed codes → T1, referenced by every refusing task · spine provenance → T4 `identity_payload`/`provenance_payload` · fictional stage → removed, replaced by T6's `UNACCOUNTED_LOGICAL_REF` completeness test.

**Acceptance gaps closed:** the probe runs in T17 rather than reading a seeded attestation · all three features have explicit hand-computed values, locally and on the cluster · `ratio_feature` is now `cross_border_value_ratio_90d` · the overlay/upload sweep runs wherever T3's shared change is in scope · T0 records the full runtime version set and feeds T12's pinned dependencies.

**Placeholder scan.** No "TBD"/"handle errors appropriately". Tasks 6, 8, 10–14 state their discriminating assertions in prose rather than full code — each applies a pattern shown in full in T2/T3/T7/T9/T16/T17, and every assertion names its exact code or behaviour.

**Type consistency.** `materialize_hash` (T1) is the sole hasher; every refusal uses a T1 enum. `AdmittedFeature` (T2) → `compile_ir` (T7). `JoinPlan` (T3) → T6. `PhysicalInputRequirement` (T5) is static and enters `ir_hash`; `PhysicalInputSnapshot` (T15) is run-time and enters only `sandbox_execution_hash`. `PhysicalType` (T8) → `PlannedFeature` (T10) → the type/nullability gate (T14). `StagingManifestV1` (T10) is rendered in T13, verified in T14. `CompilationIdentity` (T11) is embedded in rendered files; `generated_project_hash` lives only in `GENERATED.lock` (T12). `PublisherSelection` (T16) is what `render_publish` consumes.

**Unverified-interface check.** Every API named here traces to the verified-interfaces reference. If implementation meets one that does not: **read the source, add the entry, then implement.**
