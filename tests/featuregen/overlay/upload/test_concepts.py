from featuregen.overlay.upload.concepts import CONCEPTS, UNCLASSIFIED, is_known_concept, humanize


def test_vocabulary_is_controlled():
    assert "monetary_amount" in CONCEPTS
    assert "account_identifier" in CONCEPTS
    assert UNCLASSIFIED not in CONCEPTS          # the fallback is not itself a concept
    assert is_known_concept("monetary_amount") is True
    assert is_known_concept("made_up_thing") is False


def test_humanize_for_search():
    assert humanize("monetary_amount") == "monetary amount"
