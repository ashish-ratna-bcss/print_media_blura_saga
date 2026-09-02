"""
Query refinements: exclusions, proximity, timeframes, weekdays.

These run in two places -- as a MongoDB filter over the archive and as a
Python predicate over articles just read off a page -- so the cases that
matter most are the ones where the two could disagree.
"""

import datetime as dt

import pytest

from epaper import query


# -- exclusions -------------------------------------------------------------

def test_parse_terms_splits_and_normalises():
    assert query.parse_terms("traffic, Parking ,  challan") == [
        "traffic", "parking", "challan"]


def test_parse_terms_drops_duplicates_and_blanks():
    assert query.parse_terms("traffic,,traffic , ") == ["traffic"]


def test_parse_terms_accepts_a_list():
    """A stored spec round-trips as a list; a payload arrives as a string."""
    assert query.parse_terms(["Traffic", "parking"]) == ["traffic", "parking"]


def test_exclusion_matches_whole_words_only():
    """
    "car" must not exclude "carnival".

    Over-filtering by substring is the kind of bug nobody reports -- they
    conclude the search is bad and stop using it.
    """
    assert query.excluded_by("The carnival drew huge crowds", ["car"]) == ""
    assert query.excluded_by("A car hit the divider", ["car"]) == "car"


def test_exclusion_matches_a_multi_word_term_in_sequence():
    assert query.excluded_by("heavy traffic jam on NH-1", ["traffic jam"]) == "traffic jam"
    assert query.excluded_by("traffic was light; a jam of people", ["traffic jam"]) == ""


def test_exclusion_reports_which_term_hit():
    """The reason is shown to the user, so it has to be the actual term."""
    assert query.excluded_by("parking dispute", ["traffic", "parking"]) == "parking"


def test_exclusion_works_on_devanagari():
    assert query.excluded_by("दिल्ली में यातायात बाधित", ["यातायात"]) == "यातायात"
    assert query.excluded_by("दिल्ली में पुलिस", ["यातायात"]) == ""


def test_no_terms_excludes_nothing():
    assert query.excluded_by("anything at all", []) == ""


def test_mongo_exclusion_is_empty_without_terms():
    assert query.mongo_exclusion([]) == {}


def test_mongo_exclusion_negates_every_field():
    """
    $nor over the text fields, not a $text negation.

    MongoDB's text search treats a negated term as a scoring hint, so a
    $text-based exclusion returns the very articles it was asked to suppress.
    """
    f = query.mongo_exclusion(["traffic"], fields=("headline", "body"))
    assert "$nor" in f
    assert {c["headline"]["$regex"] for c in f["$nor"] if "headline" in c} == {r"\btraffic\b"}


# -- proximity --------------------------------------------------------------

TEXT = "The Delhi Police said on Monday that the police in Delhi would act"


def test_any_window_accepts_everything():
    assert query.within("totally unrelated", "Delhi Police", None) is True


def test_exact_phrase_requires_the_words_in_order():
    assert query.within(TEXT, "Delhi Police", 0) is True
    assert query.within("Police Delhi said", "Delhi Police", 0) is False


def test_narrow_window_rejects_scattered_words():
    """The case the feature exists for: dateline "Delhi", body "police"."""
    scattered = "Delhi, 16 August. " + ("filler " * 20) + "the police responded"
    assert query.within(scattered, "Delhi Police", 5) is False
    assert query.within(scattered, "Delhi Police", None) is True


def test_wider_window_accepts_what_a_narrow_one_rejects():
    text = "Delhi saw a large and noisy protest before police arrived"
    assert query.within(text, "Delhi Police", 5) is False
    assert query.within(text, "Delhi Police", 10) is True


def test_proximity_ignores_word_order_when_a_window_is_given():
    """Only the exact-phrase setting cares about order."""
    assert query.within("police in Delhi", "Delhi Police", 5) is True


def test_single_word_phrase_satisfies_any_window():
    """Degrades to "does the word appear" rather than rejecting everything."""
    assert query.within("the police acted", "police", 0) is True
    assert query.within("the police acted", "police", 5) is True
    assert query.within("nothing here", "police", 5) is False


def test_missing_word_fails_every_window():
    assert query.within("Delhi saw rain", "Delhi Police", 10) is False


def test_empty_text_matches_nothing():
    assert query.within("", "Delhi Police", 5) is False


@pytest.mark.parametrize("name,expected", [
    ("exact", 0), ("w5", 5), ("w10", 10), ("any", None), ("nonsense", None),
    (None, None)])
def test_window_for_maps_ui_values(name, expected):
    assert query.window_for(name) == expected


# -- timeframes -------------------------------------------------------------

TODAY = dt.date(2026, 9, 2)          # a Wednesday


@pytest.mark.parametrize("preset,first", [
    ("today", "2026-09-02"),
    ("3d", "2026-08-31"),
    ("week", "2026-08-27"),
    ("month", "2026-08-04"),
])
def test_presets_resolve_to_inclusive_ranges(preset, first):
    a, b = query.resolve_timeframe(preset, today=TODAY)
    assert (a, b) == (first, "2026-09-02")


def test_all_means_no_date_filter():
    """Distinct from an empty range, which would return nothing."""
    assert query.resolve_timeframe("all", today=TODAY) == (None, None)


def test_custom_passes_the_dates_through():
    assert query.resolve_timeframe(
        "custom", today=TODAY, date_from="2026-01-01", date_to="2026-01-31"
    ) == ("2026-01-01", "2026-01-31")


def test_custom_swaps_an_inverted_range():
    """Picking the dates in the order you think of them is a slip, not a query."""
    assert query.resolve_timeframe(
        "custom", today=TODAY, date_from="2026-01-31", date_to="2026-01-01"
    ) == ("2026-01-01", "2026-01-31")


def test_unknown_preset_falls_back_to_a_single_day():
    assert query.resolve_timeframe("whenever", today=TODAY) == (
        "2026-09-02", "2026-09-02")


def test_today_accepts_a_string():
    assert query.resolve_timeframe("today", today="2026-09-02")[0] == "2026-09-02"


# -- weekdays ---------------------------------------------------------------

def test_parse_weekdays_accepts_names():
    assert query.parse_weekdays(["Sat", "sun"]) == [5, 6]


def test_parse_weekdays_accepts_numbers():
    assert query.parse_weekdays([0, 6]) == [0, 6]


def test_all_seven_days_means_no_filter():
    """Every day selected and no day selected both mean "do not filter"."""
    assert query.parse_weekdays(list(query.DAY_NAMES)) == []
    assert query.parse_weekdays([]) == []


def test_parse_weekdays_ignores_rubbish():
    assert query.parse_weekdays(["sat", "funday", 99]) == [5]


def test_on_weekday_selects_the_right_days():
    assert query.on_weekday("2026-09-02", [2]) is True      # a Wednesday
    assert query.on_weekday("2026-09-02", [5, 6]) is False


def test_no_weekday_filter_accepts_everything():
    assert query.on_weekday("2026-09-02", []) is True


def test_undated_article_is_not_claimed_for_any_weekday():
    """
    Guessing would put it into a comparison it does not belong to.

    Uploads are stored with no date at all, so this is a real state and not a
    defensive branch.
    """
    assert query.on_weekday("", [2]) is False
    assert query.on_weekday(None, [2]) is False


def test_mongo_weekdays_is_empty_without_a_selection():
    assert query.mongo_weekdays([]) == {}


def test_mongo_weekdays_remaps_monday_zero_to_mongo_numbering():
    """Python: Monday is 0. Mongo $dayOfWeek: Sunday is 1."""
    assert query.mongo_weekdays([0])["$expr"]["$in"][1] == [2]     # Monday
    assert query.mongo_weekdays([6])["$expr"]["$in"][1] == [1]     # Sunday


# -- yesterday --------------------------------------------------------------

def test_yesterday_is_one_day_not_a_window():
    """
    A reader asking for yesterday's paper wants that paper, not today's and
    yesterday's -- unlike every other preset, which is a window ending today.
    """
    a, b = query.resolve_timeframe("yesterday", today=TODAY)
    assert a == b == "2026-09-01"


def test_yesterday_is_an_accepted_timeframe():
    assert "yesterday" in query.TIMEFRAMES
