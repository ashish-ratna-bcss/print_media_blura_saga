"""
Label normalisation: the half of topic filing that must never be wrong.

Free-form labels only stay usable because these rules hold. Every case below
is one the classifier has actually produced or plausibly will, and each is
here because getting it wrong puts a duplicate entry in the reader's picker.
"""

from __future__ import annotations

import pytest

from epaper import catdata


@pytest.mark.parametrize("raw,expect", [
    # the case that motivates the whole module
    ("Cybercrime", "cybercrime"),
    ("Cyber Crime", "cybercrime"),
    ("cyber-crime", "cybercrime"),
    ("CYBER CRIME", "cybercrime"),
    ("Cyber  Fraud", "cybercrime"),
    ("Online Fraud", "cybercrime"),
    # mechanical folding
    ("  Politics  ", "politics"),
    # Both fold to "security", not "crime": the police-desk split gave
    # deployment and bandobast a heading of their own, and "law and order" is
    # what a paper calls exactly that.
    ("Law & Order", "security"),
    ("law and order", "security"),
    # framing words a model wraps a label in
    ("the crime news", "crime"),
    ("Crime News", "crime"),
    ("A Robbery", "robbery"),
    ("Sports related", "sports"),
    ("the politics section", "politics"),
    # a topic with no alias passes through unchanged -- the free-label design
    # depends on this, and an over-eager table is how a subject disappears
    ("murder", "murder"),
    ("dengue outbreak", "dengue outbreak"),
])
def test_normalise(raw, expect):
    assert catdata.normalise(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ("अपराध", "crime"),
    ("साइबर ठगी", "cybercrime"),
    ("राजनीति", "politics"),
    ("कोर्ट", "court"),
])
def test_hindi_folds_to_english(raw, expect):
    """
    A bilingual corpus labelled in each article's own language splits every
    subject in two, neither half complete. This is what keeps them together.
    """
    assert catdata.normalise(raw) == expect


@pytest.mark.parametrize("bad", [
    "", "   ", None, 123, [], {},
    "This article is about a robbery that took place in Rohini on Monday",
    "!!!", "---",
])
def test_unusable_input_returns_empty(bad):
    """
    Returns "" rather than raising or inventing. The caller decides what an
    absent label means -- a sentence is a model that described the article
    instead of labelling it, and filing it would put a one-article entry in
    the picker forever.
    """
    assert catdata.normalise(bad) == ""


def test_normalise_is_idempotent():
    """
    Stored labels are re-normalised whenever the alias table changes, so a
    second pass must not keep shifting them. A rule that is not idempotent
    silently rewrites the archive every time it runs.
    """
    for raw in ["Cyber Crime", "the crime news", "साइबर ठगी", "Law & Order",
                "A Robbery", "Sports related", "murder"]:
        once = catdata.normalise(raw)
        assert catdata.normalise(once) == once


def test_aliases_are_not_chained():
    """
    Every alias target must itself be terminal. A -> B -> C means the table is
    wrong, and resolving the chain would hide the mistake instead of showing
    it.
    """
    for src, dst in catdata.ALIASES.items():
        assert dst not in catdata.ALIASES, (
            "%r -> %r -> %r is a chain; point %r straight at the final entry"
            % (src, dst, catdata.ALIASES[dst], src))


def test_alias_targets_survive_normalisation():
    """A target that normalises to something else would never be reachable."""
    for src, dst in catdata.ALIASES.items():
        assert catdata.normalise(dst) == dst, (
            "alias target %r (from %r) normalises to %r"
            % (dst, src, catdata.normalise(dst)))


def test_tags_are_cleaned_but_never_aliased():
    """
    Tags carry the specifics the topic is too broad to hold. Aliasing them
    would collapse "otp fraud" into "cybercrime" and throw away the whole
    reason they exist.
    """
    out = catdata.normalise_tags(["OTP Fraud", "otp fraud", " Rohini ",
                                  "Hit & Run", None, 42, "x" * 80])
    assert out == ["otp fraud", "rohini", "hit and run"]
    # "police" is an alias for the topic "crime"; as a tag it stays itself
    assert catdata.normalise_tags(["Police"]) == ["police"]


def test_tags_deduplicate_and_cap():
    assert catdata.normalise_tags(["a b", "A B", "a  b"]) == ["a b"]
    assert len(catdata.normalise_tags(["t%d" % i for i in range(50)])) == 8


def test_display_keeps_acronyms_upper():
    assert catdata.display("cybercrime") == "Cybercrime"
    assert catdata.display("fir quashed") == "FIR Quashed"
    assert catdata.display("") == ""


# -- the published scheme ---------------------------------------------------

def test_every_heading_has_a_note():
    """
    The Categories tab shows these to somebody deciding where an article
    belongs. A heading added to PARENTS and not described would appear there
    as a bare word with nothing to say what it holds.
    """
    missing = [p for p in catdata.PARENTS if not catdata.PARENT_NOTES.get(p)]
    assert missing == []


def test_no_note_describes_a_heading_that_does_not_exist():
    """A note left behind after a heading is renamed is worse than none."""
    orphans = [k for k in catdata.PARENT_NOTES if k not in catdata.PARENTS]
    assert orphans == []


def test_every_police_heading_is_a_real_parent():
    for p in catdata.POLICE_PARENTS:
        assert p in catdata.PARENTS


def test_children_of_returns_only_that_headings_seeds():
    kids = catdata.children_of("traffic")
    assert "challan" in kids
    assert all(catdata.SEED_PARENTS[c] == "traffic" for c in kids)
    assert "murder" not in kids


def test_children_of_an_unknown_heading_is_empty():
    assert catdata.children_of("nonexistent") == []


def test_children_are_sorted_so_the_panel_is_stable():
    """Rendered as a list; dict order would reshuffle it between reads."""
    kids = catdata.children_of("crime")
    assert kids == sorted(kids)


def test_no_seeded_child_points_at_a_missing_parent():
    """
    A child whose parent is not a heading can never be filed, and the article
    carrying it lands under "unclassified" with no way to see why.
    """
    bad = {c: p for c, p in catdata.SEED_PARENTS.items()
           if p not in catdata.PARENTS}
    assert bad == {}


def test_no_seeded_child_is_also_a_heading():
    """
    A label that is both would be filed under itself. seed_parent() returns
    "" for a parent, so such an entry is silently ignored -- which reads as a
    working mapping that does nothing.
    """
    both = [c for c in catdata.SEED_PARENTS if c in catdata.PARENTS
            and catdata.SEED_PARENTS[c] != c]
    assert both == [], both


def test_a_heading_is_never_its_own_child():
    """
    "traffic": "traffic" does nothing -- seed_parent() returns "" for a
    heading -- but rendered in the scheme panel it read as traffic being a
    subcategory of traffic.
    """
    for p in catdata.PARENTS:
        assert p not in catdata.children_of(p)


def test_no_alias_rewrites_a_heading_into_another_heading():
    """
    The bug the police split introduced.

    ALIASES predates the five police headings and mapped "policing" onto
    "crime". So when the classifier answered "policing" -- correctly -- it was
    folded into "crime" before anything downstream saw it, which is most of
    why the heading looked unlearnable in measurement.

    A heading must survive normalisation as itself.
    """
    broken = {p: catdata.normalise(p) for p in catdata.PARENTS
              if catdata.normalise(p) != p}
    assert broken == {}, broken


def test_police_words_file_under_the_right_desk():
    assert catdata.normalise("police") == "policing"
    assert catdata.normalise("law and order") == "security"
    assert catdata.normalise("पुलिस") == "policing"
