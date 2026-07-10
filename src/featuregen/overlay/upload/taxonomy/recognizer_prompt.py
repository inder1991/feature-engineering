"""Phase-1A Task 2 — the recognition prompt builder.

Pure, deterministic string assembly (no ``Date``/random): the closed *selectable* taxonomy + the
classification rules + the required output JSON shape. The recognizer (``recognizer.py``) sends this
as the task prompt; the redacted hypothesis/goal ride in the request ``inputs`` (never embedded here,
and never any catalog columns).

The taxonomy listing is built from ``selectable_leaves()`` — the terminal, choosable objectives — so
the non-selectable ``financial_crime`` domain parent (and any intermediate family that has a
selectable child) is never offered as a pick, only its selectable branches are.

Behaviour-neutral: read-only over the taxonomy registry; nothing here touches ``templates.py`` or
grounding. See ``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 2.
"""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.dimensions import MODELLING_CONTEXTS, known_entities
from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves, use_case

PROMPT_ID = "use_case_recognition"
# Bumped to "2" for Phase-2B/B1: the prompt now enumerates the two dimension vocabularies (modelling
# contexts + target entities), their classification rules, and the extended output shape — a material
# change to what every intent_recognition_attempt was produced under, so replay must not read it as "1".
PROMPT_VERSION = "2"


def _taxonomy_lines() -> list[str]:
    """One line per selectable objective: ``- <id> — <display_name>`` plus, where the node carries
    boundary examples, a compact ``(e.g. <include>; not <exclude>)`` disambiguation hint. Only
    selectable objectives are listed — the recognizer may return no other id."""
    lines: list[str] = []
    for uid in selectable_leaves():
        node = use_case(uid)
        if node is None:  # defensive: selectable_leaves() only yields registry ids
            continue
        line = f"- {node.id} — {node.display_name}"
        if node.include_examples and node.exclude_examples:
            line += f" (e.g. {node.include_examples[0]}; not {node.exclude_examples[0]})"
        elif node.include_examples:
            line += f" (e.g. {node.include_examples[0]})"
        lines.append(line)
    return lines


def build_recognition_prompt() -> str:
    """Assemble the deterministic recognition prompt: the closed selectable taxonomy, the two optional
    intent-dimension vocabularies (modelling contexts + target entities), the classification rules, and
    the required output JSON shape. No input values are embedded — the redacted hypothesis/prediction
    goal are supplied separately as the request inputs. Deterministic: same output on every call (the
    closed vocabularies are sorted, so no set-ordering nondeterminism; no clock, no randomness)."""
    taxonomy = "\n".join(_taxonomy_lines())
    contexts = ", ".join(sorted(MODELLING_CONTEXTS))
    entities = ", ".join(sorted(known_entities()))
    return (
        "You classify a bank feature-engineering request into governed use-case objectives drawn "
        "from a CLOSED taxonomy. Classify from the STATED OBJECTIVE of the request (what is being "
        "predicted or decided), NOT from whatever data or columns happen to be available.\n\n"
        "CLOSED TAXONOMY — the only use_case_id values you may return:\n"
        f"{taxonomy}\n\n"
        "CLOSED MODELLING CONTEXTS — the regulatory framework / modelling regime the request sits "
        "under (return 0 or more, from THIS list only):\n"
        f"{contexts}\n\n"
        "CLOSED TARGET ENTITIES — the grain the prediction is ABOUT, i.e. the entity each row of the "
        "prediction represents (return exactly ONE from THIS list, or null):\n"
        f"{entities}\n\n"
        "RULES:\n"
        "- Choose at most ONE primary objective and at most TWO secondary objectives, using ONLY "
        "the use_case_id values listed above. Never invent an id, and never return an id that is not "
        "listed.\n"
        "- relationship is \"primary\" (the single best-fit objective) or \"secondary\" (a genuinely "
        "supporting objective).\n"
        "- confidence is a qualitative band: \"high\", \"medium\", or \"low\".\n"
        "- evidence_spans must quote short verbatim spans FROM THE INPUT that justify each pick.\n"
        "- modelling_contexts: return the modelling FRAMEWORK/REGIME(S) the objective is being modelled "
        "under (e.g. an IFRS9 expected-credit-loss framing -> \"ifrs9\"), classified from the stated "
        "INTENT, not from the available data. Return an empty list when none clearly applies.\n"
        "- target_entity: return the single PREDICTION GRAIN — the entity each prediction is about "
        "(e.g. a per-customer churn score -> \"customer\") — again from the intent, not the data. "
        "Return null when no listed entity clearly fits; NEVER invent one.\n"
        "- Return status \"unscoped\" with an empty candidates list when no listed id clearly "
        "applies, or the request is exploratory / has no stated prediction target.\n"
        "- Classify from the stated objective, not from the available data.\n\n"
        "OUTPUT — return a single JSON object of exactly this shape:\n"
        "{\n"
        "  \"status\": \"classified\" | \"ambiguous\" | \"unscoped\",\n"
        "  \"candidates\": [\n"
        "    {\n"
        "      \"use_case_id\": \"<one of the listed ids>\",\n"
        "      \"relationship\": \"primary\" | \"secondary\",\n"
        "      \"confidence\": \"high\" | \"medium\" | \"low\",\n"
        "      \"evidence_spans\": [\"<verbatim quote from the input>\"],\n"
        "      \"rationale\": \"<one sentence on why this objective fits>\"\n"
        "    }\n"
        "  ],\n"
        "  \"modelling_contexts\": [\"<zero or more of the listed modelling contexts>\"],\n"
        "  \"target_entity\": \"<one of the listed entities, or null>\",\n"
        "  \"ambiguity_note\": \"<optional: set when the request is ambiguous between objectives>\"\n"
        "}\n"
    )
