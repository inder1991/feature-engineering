"""Released identity versions for the taxonomy inputs shared by recognition and planning."""

APPLICABILITY_MAPPING_VERSION = "2.0.0"
RECIPE_REGISTRY_VERSION = "2.0.0"
# The presentation-priority ranker's OWN mapping identity — the recipe universe its signal profiles
# are derived from plus the derivation of each axis. Split out of APPLICABILITY_MAPPING_VERSION on
# 2026-08-14, when the ranker was re-keyed from the legacy Template registry onto the V2 recipe
# registry: that changed which recipes can be ranked and where each signal is read from, and a
# ranking stamped with the applicability version could not say so. Applicability did not change,
# so its version must not move; conflating the two made either bump a lie about the other.
RANKING_MAPPING_VERSION = "ranking-v2-recipes@1"
CONCEPT_REGISTRY_VERSION = "concepts@3"   # BR-10: +35 banking event/lifecycle concepts (§3.20)
# Suggestion-discovery Task 1 (freeze 0F-6: new registries pin their versions here).
FEATURE_CATEGORY_REGISTRY_VERSION = "feature-categories@1"
RECIPE_FAMILY_REGISTRY_VERSION = "recipe-families@1"
TEMPLATE_DISCOVERY_REGISTRY_VERSION = "template-discovery@1"
