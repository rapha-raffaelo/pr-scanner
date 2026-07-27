"""Outlet tiers (newspulse.outlets).

Tier is a ranking signal only — see the module docstring for why weighting the
score itself was measured and rejected. These tests pin the lookup's normalization
(feeds spell one outlet many ways) and the default-tier promise.
"""

from __future__ import annotations

import pytest

from newspulse import outlets


@pytest.mark.parametrize(
    "spelling",
    ["SZ.de", "sz", "SZ", "  sz.DE  "],
)
def test_one_outlet_is_recognised_however_a_feed_spells_it(spelling):
    """Case, whitespace and a trailing domain suffix are not identity."""
    assert outlets.tier_for(spelling) == 1


def test_umlaut_spellings_collapse_to_one_outlet():
    """German outlets appear both as "Börse Online" and "Boerse Online"."""
    assert outlets.normalize_outlet("Börse Online") == outlets.normalize_outlet("Boerse Online")
    assert outlets.tier_for("Börse Online") == 3
    assert outlets.tier_for("Boerse Online") == 3


def test_punctuation_is_not_identity():
    assert outlets.tier_for("wallstreet:online") == 3
    assert outlets.tier_for("wallstreet-online") == 3


def test_unlisted_outlet_gets_the_neutral_default_tier():
    """Most of what the per-client search finds is genuine regional press; being
    unlisted must never be a penalty."""
    assert outlets.tier_for("Oberberg-Aktuell") == outlets.DEFAULT_TIER
    assert outlets.adjustment_for("Oberberg-Aktuell") == 0
    assert outlets.effective_importance(7, "Oberberg-Aktuell") == 7


def test_missing_or_blank_source_is_treated_as_default_not_an_error():
    assert outlets.tier_for(None) == outlets.DEFAULT_TIER
    assert outlets.tier_for("") == outlets.DEFAULT_TIER


def test_tiers_rank_leitmedien_above_default_above_wires():
    """A lower tier number is the better outlet — the ordering the feed sorts on."""
    assert outlets.tier_for("FAZ") < outlets.tier_for("Oberberg-Aktuell")
    assert outlets.tier_for("Oberberg-Aktuell") < outlets.tier_for("Ad-hoc-news.de")


def test_effective_importance_stays_inside_the_model_scale():
    """A weighted score must remain comparable with a raw one, and renderable."""
    assert outlets.effective_importance(10, "FAZ") == 10       # would be 11
    assert outlets.effective_importance(0, "Ad-hoc-news.de") == 0  # would be -2
    assert outlets.effective_importance(5, "Ad-hoc-news.de") == 3
    assert outlets.effective_importance(5, "FAZ") == 6
