"""Task 0.6 Seam 3a (D10) — the schema-registry inventory gate.

An audited structured call now RAISES when its requested ``(schema_id, schema_version)`` does not
resolve in the registry (never dispatching unenforced). That makes an unregistered pair a RUNTIME
crash — so this suite makes it a CI failure instead: it statically scans every call site in
``src/featuregen`` for the pairs it can request (literal, module-constant, f-string fallback, or a
known version-helper call) and asserts each one resolves after the owners' registration hooks run.
Bumping a version at a call site without registering the schema body fails HERE, before dispatch.
"""
from __future__ import annotations

import ast
from pathlib import Path

import featuregen
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.formula.author import AUTHOR_TURN_CONTRACT_V2, _register_turn_schema
from featuregen.formula.critic import _register_critic_schema
from featuregen.overlay.upload import feature_assist
from featuregen.overlay.upload.enrich_llm import register_enrichment_schemas
from featuregen.overlay.upload.semantic_bindings.enrich import _register_selection_schema

SRC = Path(featuregen.__file__).resolve().parent

# Version-helper calls a site may pass as ``schema_version=``, mapped to EVERY value the helper can
# return. A new helper (or a widened return set) must be registered here or the scan raises.
_VERSION_FUNCS = {
    "_feature_schema_version": lambda: {1, feature_assist._FEATURE_CONTEXT_SCHEMA_VERSION},
}

# Wrapper functions that take schema_id POSITIONALLY: name -> 0-based index of schema_id. Their
# schema_version arrives as a keyword (scanned on the same call) or defaults to 1.
_POSITIONAL_WRAPPERS = {
    "_call": 4,       # enrich.py     (conn, client, task, prompt_id, schema_id, ...)
    "_call_raw": 4,   # feature_assist.py (conn, client, task, prompt_id, schema_id, ...)
}

# The one dynamic schema_id construction: enrich_batch._single_fallback's
# f"overlay_{single_prompt}" — the per-item fallback of every NON-ref-aware run_batched task.
# Expanded to the flat single schemas those tasks resolve to, at EVERY version a fallback-capable
# caller can thread (`_single_fallback` passes the batch call's own `schema_version` straight
# through). `overlay_domain` reaches v2 from the D13.2 sub-domain batch; ref-aware tasks (Pass B)
# never reach the fallback at all.
_DYNAMIC_FALLBACK_PAIRS = {
    ("overlay_concept", 1), ("overlay_definition", 1),
    ("overlay_domain", 1), ("overlay_domain", 2),
    ("overlay_synonyms", 1), ("overlay_unit", 1), ("overlay_summary", 1),
}
_DYNAMIC = "__DYNAMIC__"


def _module_constants(tree: ast.Module) -> dict[str, object]:
    consts: dict[str, object] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, (str, int))):
            consts[node.targets[0].id] = node.value.value
    return consts


def _module_dict_names(tree: ast.Module, known: set[str]) -> set[str]:
    """Names bound at module level to a dict LITERAL (a schema BODY, e.g. ``INTENT_SCHEMA``),
    including plain-name aliases of one (``CRITIC_SCHEMA = CRITIC_FINDINGS_V1_SCHEMA``). Used to
    recognize an ``LLMRequest``/contract construction that carries its schema body INLINE — such a
    request never consults the registry, so it imposes no registration to gate on."""
    names: set[str] = set(known)
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Dict) or (isinstance(value, ast.Name) and value.id in names):
            names.add(target.id)
    return names


def _resolve(node: ast.expr, consts: dict[str, object]) -> set | None:
    """The set of values this schema kwarg can take; ``None`` for a pure pass-through (a bare Name
    with no known constant binding — a wrapper parameter, whose ORIGINATING sites are scanned
    themselves). Attribute access (a runtime-supplied provider contract) contributes nothing: its
    producer registered the pair when it minted the contract."""
    if isinstance(node, ast.Constant):
        return {node.value}
    if isinstance(node, ast.Name):
        return {consts[node.id]} if node.id in consts else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _VERSION_FUNCS):
        return set(_VERSION_FUNCS[node.func.id]())
    if isinstance(node, ast.IfExp):
        out: set = set()
        for branch in (node.body, node.orelse):
            resolved = _resolve(branch, consts)
            out |= resolved or set()
        return out
    if isinstance(node, ast.JoinedStr):
        return {_DYNAMIC}
    if isinstance(node, ast.Attribute):
        return set()
    if isinstance(node, ast.Subscript):
        return set()   # a stored request being rehydrated (row["…"]) — its producer owns the pair
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("str", "int")):
        return set()   # str()/int() over a stored value — same rehydration disposition
    raise AssertionError(f"unhandled schema kwarg shape: {ast.dump(node)}")


def _requested_pairs() -> set[tuple[str, int]]:
    global_consts: dict[str, object] = {}
    dict_names: set[str] = set()
    trees: list[ast.Module] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        trees.append(tree)
        # Name-resolution fallback for constants imported from a sibling module (first def wins).
        for name, value in _module_constants(tree).items():
            global_consts.setdefault(name, value)
        dict_names |= _module_dict_names(tree, dict_names)

    pairs: set[tuple[str, int]] = set()
    for tree in trees:
        consts = {**global_consts, **_module_constants(tree)}
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            sid_node: ast.expr | None = kw.get("schema_id")
            vers_key = "schema_version"
            if (sid_node is None and isinstance(call.func, ast.Name)
                    and call.func.id in _POSITIONAL_WRAPPERS):
                idx = _POSITIONAL_WRAPPERS[call.func.id]
                if len(call.args) > idx:
                    sid_node = call.args[idx]
            if sid_node is None and "output_schema_id" in kw:
                # ``output_schema_id=`` sites (LLMRequest / provider-contract construction, e.g. the
                # semantic-binding SELECT). A call whose ``output_schema=`` names a module-level
                # schema BODY carries its schema inline — the registry is never consulted for it —
                # so only registry-resolved requests gate here.
                schema_node = kw.get("output_schema")
                if not (isinstance(schema_node, ast.Name) and schema_node.id in dict_names):
                    sid_node = kw["output_schema_id"]
                    vers_key = "output_schema_version"
            if sid_node is None:
                continue
            sids = _resolve(sid_node, consts)
            if sids is None:
                continue   # pass-through wrapper — originating sites are scanned directly
            if _DYNAMIC in sids:
                # the f-string fallback site: its pairs carry their own (expanded) versions
                pairs |= _DYNAMIC_FALLBACK_PAIRS
                sids = sids - {_DYNAMIC}
            if not sids:
                continue
            vers_node = kw.get(vers_key)
            vers = {1} if vers_node is None else _resolve(vers_node, consts)
            if vers is None:
                continue   # version threaded from the caller — that caller is scanned directly
            for sid in sids:
                for ver in vers:
                    assert isinstance(sid, str) and isinstance(ver, int), (sid, ver)
                    pairs.add((sid, ver))
    return pairs


def test_the_scan_sees_the_known_call_sites():
    """Guard the gate itself: if a refactor makes the static scan miss the core seams, this fails
    LOUDLY instead of the resolution test passing vacuously."""
    pairs = _requested_pairs()
    for expected in [
        ("overlay_concept", 1),                       # enrich.py single mode (positional _call)
        ("overlay_concept_batch", 1),                 # enrich.py batch mode
        ("overlay_summary", 1),                       # the dynamic single-fallback expansion
        # Pass B ships the profile-synthesis contract at schema v3 (a REAL body — v2 is a
        # byte-alias of v1 and would reject every profile field). The version arrives as the
        # module constant `_SYNTH_SCHEMA_VERSION`, which the scan resolves.
        ("overlay_table_synth_batch", 3),
        ("feature_ideas", 1),                         # feature_assist flag-off
        ("feature_ideas", feature_assist._FEATURE_CONTEXT_SCHEMA_VERSION),   # flag-on
        ("overlay_contract", 1),                      # contract author/refine
        ("concept_critique", 1), ("concept_revision", 1),   # concept critic
        ("bridge_identifier_critique", 1),            # bridge critic
        ("use_case_recognition", 1),                  # recognizer
        # the output_schema_id= widening: the semantic-binding SELECT resolves its schema from
        # the registry (LLMRequest kwarg — the previously unscanned site).
        ("overlay_semantic_bindings_select", 1),
    ]:
        assert expected in pairs, f"static scan no longer sees {expected}"


def test_every_requested_schema_pair_resolves(db):
    """The gate: every statically-reachable (schema_id, schema_version) resolves once the owners'
    registration hooks have run — a version bump without a registered body fails CI here, not as a
    live dispatch crash."""
    register_enrichment_schemas(db)
    _register_turn_schema(db)                       # the v1 author turn contract (the default)
    _register_turn_schema(db, AUTHOR_TURN_CONTRACT_V2)   # Task A3 — the v2 author turn contract
    _register_critic_schema(db)
    _register_selection_schema(db)
    reg = DocumentSchemaRegistry(db)
    missing = sorted(
        (sid, ver) for sid, ver in _requested_pairs() if reg.schema_for(sid, ver) is None)
    assert missing == [], f"requested but unregistered schema pairs: {missing}"
