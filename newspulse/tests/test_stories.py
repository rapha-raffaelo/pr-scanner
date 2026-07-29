"""Story clustering (newspulse.stories).

The failure that matters here is asymmetric: under-grouping shows a duplicate,
over-grouping *hides a story a human never sees*. The tests are weighted
accordingly — most of them assert that things stay apart.

Headlines are taken from real German coverage, including the dpa wire copy whose
section labels ("Online-Händler: …", "… - Wirtschaft - SZ.de") are exactly what
defeats a plain title-hash comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

from newspulse.stories import cluster


@dataclass
class Row:
    headline: str
    source: str
    importance: int = 5


def _counts(rows):
    return sorted((s.pickup_count for s in cluster(rows)), reverse=True)


# --- The case this exists for --------------------------------------------------

_BAFIN = [
    Row("Online-Händler - Bafin rügt Zalando wegen fehlender Angaben zur Übernahme - Wirtschaft - SZ.de", "SZ.de", 8),
    Row("Online-Händler: Bafin rügt Zalando wegen fehlender Angaben zur Übernahme - Tagesspiegel", "Tagesspiegel", 8),
    Row("Bafin rügt Zalando wegen fehlender Angaben zur Übernahme - Baden Online", "Baden Online", 8),
]


def test_wire_copy_carrying_different_section_labels_is_one_story():
    stories = cluster(_BAFIN)
    assert len(stories) == 1
    assert stories[0].pickup_count == 3
    assert set(stories[0].outlets) == {"SZ.de", "Tagesspiegel", "Baden Online"}


def test_the_lead_is_the_first_item_the_caller_ranked():
    """Input order is the caller's ranking, so the story sorts where its best
    article would have."""
    story = cluster(_BAFIN)[0]
    assert story.lead.source == "SZ.de"


def test_the_same_outlet_twice_is_not_two_pickups():
    rows = _BAFIN + [Row(_BAFIN[0].headline, "SZ.de", 8)]
    story = cluster(rows)[0]
    assert story.pickup_count == 3


# --- What must stay apart ------------------------------------------------------


def test_a_different_angle_on_the_same_event_stays_its_own_story():
    """Real coverage of one event, written up independently. Merging these would
    hide a distinct piece of coverage behind another outlet's headline."""
    rows = _BAFIN + [
        Row("Harte Tage für Zalando: Bafin rügt Geschäftsbericht – Aktie gibt nach", "Capital.de", 8),
        Row("Nach Prüfung: Bafin legt Fehler im Zalando-Abschluss offen", "finance-magazin.de", 9),
    ]
    assert _counts(rows) == [3, 1, 1]


def test_unrelated_stories_about_one_client_never_merge():
    rows = [
        Row("Zalando schließt Standort Erfurt, 2100 Jobs fallen weg", "MDR.de", 9),
        Row("Zalando startet Same-Day-Lieferung in fünf weiteren Städten", "Spiegel", 5),
        Row("Zalando beruft neue Finanzvorständin zum Quartalsende", "FAZ", 7),
    ]
    assert _counts(rows) == [1, 1, 1]


def test_thin_headlines_are_never_grouped():
    """Too little signal to trust: two unrelated short labels look alike."""
    rows = [
        Row("Zalando Rückruf", "FAZ"),
        Row("Zalando Rückruf", "Spiegel"),
    ]
    assert _counts(rows) == [1, 1]


def test_a_shared_outlet_name_alone_does_not_group():
    """The outlet's own tokens are removed before comparison, so two unrelated
    stories from one publisher do not look similar just by sharing its byline."""
    rows = [
        Row("Erste vollkommen andere Meldung über Logistikzentren - Handelsblatt", "Handelsblatt"),
        Row("Zweite komplett verschiedene Nachricht zu Quartalszahlen - Handelsblatt", "Handelsblatt"),
    ]
    assert _counts(rows) == [1, 1]


# --- Shape ---------------------------------------------------------------------


def test_every_article_appears_in_exactly_one_story():
    rows = _BAFIN + [
        Row("Zalando schließt Standort Erfurt, 2100 Jobs fallen weg", "MDR.de", 9),
        Row("Zalando startet Same-Day-Lieferung in fünf weiteren Städten", "Spiegel", 5),
    ]
    stories = cluster(rows)
    assert sum(len(s.members) for s in stories) == len(rows)


def test_a_single_article_is_a_story_that_is_not_syndicated():
    story = cluster([Row("Zalando eröffnet neues Logistikzentrum in Polen", "FAZ")])[0]
    assert story.pickup_count == 1
    assert story.is_syndicated is False


def test_empty_input_yields_no_stories():
    assert cluster([]) == []
