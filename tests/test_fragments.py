"""
Page furniture is recognised, and real stories are not.

The false-positive cases matter more than the true positives here. Marking a
dateline as furniture saves a classifier call; marking a story as furniture
loses it from the archive, and nobody finds out until they go looking for an
article that was never indexed. Every headline in KEEP is one this detector
took, or nearly took, on a real front page.
"""

import pytest

from epaper import fragments


# Real blocks from the archive that must never be marked. The numbers are the
# trap: a dateline ends at a date, and so does "fetch record 16.2 cr".
KEEP = [
    "Gandhis 688 handwritten notes to freedom fighter fetch record 16.2 cr",
    "Pb & Hry HC set for 10 lakh sq ft expansion to tackle infra crunch",
    "Retestof3UGC-NET papers over errors",
    "Extradite Hasina: Dhaka sets pre-condition for Rahman visit",
    "BJP rejigon cards as Nabin finalises team",
    "बुनियादी cid से हटकर होगा 10 लाख वर्ग फुट परिसर विस्तार",
    "मोदी की 'दिमागी नक्सल” टिप्पणी पर मचा सियासी बवाल",
    "झारखंड सरकार और छात्रों में आज फिर होगी वार्ता",
    # Short, and Hindi. Counting only str.isalpha() scored these below the
    # noise threshold because the vowel marks are not letters to Python.
    "ऐसे करता है काम",
    "यह कहा था पीएम ने",
]

DROP = [
    ("eft दिल्‍ली, 16 अगस्त (ट्रिन्यू)", "dateline"),
    ("मुंबई, 16 अणस्त (एजेंसी)", "dateline"),
    ("New Delhi, 16 August (PTI)", "dateline"),
    ("नरेंद्र जेठी।निस नरवाना 6 अगस्त", "byline"),
    ("ae thei", "noise"),
    ("iene", "noise"),
    ("HES 7 ee EEE", "noise"),
    # The ellipsis rule outranks the noise rule: this is the tail of a
    # story whose beginning was detected as a separate block.
    ("...", "continuation"),
]


@pytest.mark.parametrize("headline", KEEP)
def test_real_headlines_survive(headline):
    assert fragments.reason({"headline": headline, "body": ""}) == ""


@pytest.mark.parametrize("headline,why", DROP)
def test_furniture_is_marked(headline, why):
    assert fragments.reason({"headline": headline, "body": ""}) == why


def test_body_text_outranks_every_rule():
    """A block with a real story in it is a story, whatever its first line."""
    a = {"headline": "मुंबई, 16 अणस्त (एजेंसी)", "body": "x" * 200}
    assert fragments.reason(a) == ""


def test_ads_and_mastheads_are_left_alone():
    """Already handled elsewhere; this must not relabel them."""
    for t in ("ad", "masthead"):
        assert fragments.reason({"type": t, "headline": "ae thei",
                                 "body": ""}) == ""


def test_devanagari_marks_count_as_letters():
    """str.isalpha() is False for the vowel signs; the count must not be."""
    assert fragments._letters("मुंबई") == 5
    assert fragments._letters("mumbai") == 6


def test_split_marks_in_place_without_dropping():
    arts = [{"type": "article", "headline": "A real headline about a thing",
             "body": ""},
            {"type": "article", "headline": "ae thei", "body": ""},
            {"type": "ad", "headline": "", "body": ""}]
    stories, furniture = fragments.split(arts)

    assert len(stories) == 1
    assert len(furniture) == 1
    # Marked, not removed: the caller's list still holds every block, so the
    # Result view can still draw them.
    assert len(arts) == 3
    assert arts[1]["type"] == fragments.FRAGMENT
    assert arts[1]["fragment_reason"] == "noise"
    assert arts[2]["type"] == "ad"


def test_split_is_idempotent():
    """Re-running over an already-marked list must not reclassify anything."""
    arts = [{"type": "article", "headline": "ae thei", "body": ""}]
    fragments.split(arts)
    stories, furniture = fragments.split(arts)
    # Already-marked furniture comes back as furniture, never as a story --
    # otherwise a second pass would hand it to the classifier.
    assert stories == []
    assert furniture == [arts[0]]
    assert arts[0]["type"] == fragments.FRAGMENT
