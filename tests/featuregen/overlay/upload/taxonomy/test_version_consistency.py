from featuregen.overlay.upload.planner import contracts
from featuregen.overlay.upload.taxonomy import recognition, versions


def test_taxonomy_versions_have_one_shared_source() -> None:
    assert {
        recognition.APPLICABILITY_MAPPING_VERSION,
        contracts.APPLICABILITY_MAPPING_VERSION,
        versions.APPLICABILITY_MAPPING_VERSION,
    } == {versions.APPLICABILITY_MAPPING_VERSION}
    assert {
        recognition.RECIPE_REGISTRY_VERSION,
        contracts.RECIPE_REGISTRY_VERSION,
        versions.RECIPE_REGISTRY_VERSION,
    } == {versions.RECIPE_REGISTRY_VERSION}
    assert contracts.CONCEPT_REGISTRY_VERSION == versions.CONCEPT_REGISTRY_VERSION
