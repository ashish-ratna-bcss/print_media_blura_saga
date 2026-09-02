"""
The five police headings, arbitrated by wording rather than by the model.

Measured on qwen2.5vl:3b: it separates these 61% of the time with all twenty
headings offered, and 60% when offered only these five -- so the failure is
its grasp of the distinction, not the size of the choice. These rules take
the same cases to 94%.

The negative tests carry the weight. A rule that pulls "Court grants bail to
man arrested" onto the police desk has not improved anything; it has moved
the error. Every heading this module can produce is one it must also know
when to leave alone.
"""

import pytest

from epaper import policedesk as pd


# What the desk should conclude from wording alone.
DESK_CASES = [
    # traffic: enforcement of road rules
    ("12,000 challans issued in week-long drive", "traffic"),
    ("Traffic police launch drive against drunk driving", "traffic"),
    ("चालान काटे गए नशे में गाड़ी चलाने पर", "traffic"),

    # policing: the force as an organisation
    ("Three SHOs transferred in district reshuffle", "policing"),
    ("Inspector Ravi Kumar among 34 cops honoured for gallantry", "policing"),
    ("Khanna police turn to facial recognition to track criminals", "policing"),
    ("Recruitment drive for 2,000 constables announced", "policing"),
    ("तीन थाना प्रभारियों का तबादला", "policing"),

    # investigation: the case being worked
    ("Man arrested for cheating 40 people of Rs 2 crore", "investigation"),
    ("1,500-page chargesheet filed in fire tragedy", "investigation"),
    ("Cyber cell recovers Rs 18 lakh lost to OTP fraud", "investigation"),
    ("हत्या के आरोपी को गिरफ्तार किया गया", "investigation"),

    # security: keeping order
    ("Heavy deployment ahead of Sunday rally", "security"),
    ("Section 144 imposed in three districts", "security"),
    ("धारा 144 लागू", "security"),

    # crime: the offence is the news
    ("Woman robbed at knifepoint in Sector 22", "crime"),
    ("हत्या से इलाके में सनसनी", "crime"),
]


@pytest.mark.parametrize("headline,want", DESK_CASES)
def test_wording_fixes_the_heading(headline, want):
    assert pd.desk_heading(headline) == want


def test_an_arrest_for_a_murder_is_an_arrest_story():
    """
    Ordering is the rule. Both cues are present and investigation is tested
    first, because a police desk searching arrests wants the case rather than
    the original crime report.
    """
    assert pd.desk_heading("Man arrested for murder of shopkeeper") == "investigation"


def test_nothing_police_shaped_returns_nothing():
    for h in ("India beat Australia by five wickets",
              "Sensex climbs 400 points", "Monsoon reaches Kerala early", ""):
        assert pd.desk_heading(h) == ""


# -- refine: what it must NOT touch ----------------------------------------

NEGATIVES = [
    # A bail hearing names an arrest; the story is the court's decision.
    ("Court grants bail to man arrested in fraud case", "court"),
    ("Supreme Court verdict on police reform petition", "court"),
    ("HC stays transfer of three officers", "court"),
    # A crash names the police who attended; the story is the crash.
    ("Six killed as bus rams into truck near toll plaza", "accident"),
    ("Two killed as car rams into truck-trailer on NH-1", "accident"),
]


@pytest.mark.parametrize("headline,said", NEGATIVES)
def test_a_court_or_accident_story_is_left_alone(headline, said):
    assert pd.refine(said, headline) == said


@pytest.mark.parametrize("said", ["sports", "business", "health", "agriculture"])
def test_headings_outside_the_desk_are_never_second_guessed(said):
    """
    There is no rule here that could check "sports". Replacing one unverified
    label with another is not an improvement, so the module stays silent.
    """
    assert pd.refine(said, "India beat Australia by five wickets") == said


def test_a_non_desk_answer_is_pulled_back_only_by_the_headline():
    """
    The body of a court report recounts the arrest at length. Letting a body
    mention decide would refile every bail hearing as an investigation.
    """
    body = "The accused had been arrested in March and remanded to custody."
    assert pd.refine("technology", "Cyber cell recovers Rs 18 lakh", "") == "investigation"
    assert pd.refine("court", "Verdict reserved in land case", body) == "court"


def test_a_desk_answer_is_corrected_within_the_desk():
    assert pd.refine("crime", "Three SHOs transferred in reshuffle") == "policing"
    assert pd.refine("crime", "Man arrested after two-month manhunt") == "investigation"


def test_an_empty_answer_is_filled_from_the_wording():
    assert pd.refine("", "12,000 challans issued") == "traffic"


def test_a_desk_answer_yields_to_a_plain_accident_headline():
    """
    Adding a "traffic" heading pulled road crashes away from "accident" --
    a regression the rules have to undo, since the model now has a plausible
    wrong answer it did not have before.
    """
    assert pd.refine("traffic", "Two killed as car rams into truck") == "accident"


def test_every_heading_it_can_produce_is_a_real_parent():
    """A heading this module invents but catdata does not know is unfilable."""
    from epaper import catdata
    for head in pd.DESK:
        assert head in catdata.PARENTS
