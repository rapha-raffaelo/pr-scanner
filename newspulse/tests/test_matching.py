"""Unit tests for client matching and deduplication.

These exercise the two public entry points directly with plain in-memory objects:

* ``match_candidates`` — pairs feed items with clients via the word-boundary
  pre-filter. The load-bearing cases are the compound-noun rejection (the rule
  that keeps "Bahn" out of "Autobahn") and an alias hit.
* ``deduplicate`` — drops already-stored URLs and collapses near-duplicate wire
  copy, keeping a deterministic copy.

No database, no network, no subprocess: matching and dedup are pure functions over
``FeedItem``-shaped inputs, so a light-weight fake client (just the attributes the
matcher reads) is enough and keeps every test well under a second.
"""

from __future__ import annotations

import datetime as dt

from newspulse import matching
from newspulse.ingest import FeedItem
from newspulse.matching import Candidate

_WHEN = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)


class FakeClient:
    """Only the fields the matcher reads (name, aliases, keywords). Standing in
    for the ORM ``Client`` so these stay pure unit tests with no DB session."""

    def __init__(
        self,
        name: str,
        *,
        aliases: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> None:
        self.name = name
        self.aliases = aliases or []
        self.keywords = keywords or []


def _item(
    title: str,
    link: str,
    *,
    source: str = "Beispiel",
    summary: str | None = None,
    when: dt.datetime | None = None,
) -> FeedItem:
    return FeedItem(
        title=title,
        link=link,
        source=source,
        published_at=when or _WHEN,
        summary=summary,
        language="de",
    )


# --- match_candidates: word-boundary rule --------------------------------------


def test_compound_noun_substring_is_not_a_match():
    """The core false-positive the word-boundary rule must reject: a client named
    "Bahn" appears as a substring inside the German compound "Autobahn", which is
    a different word and must NOT count as a hit."""
    client = FakeClient("Bahn")
    item = _item("Neuer Abschnitt der Autobahn A7 eröffnet", "https://x.de/a7")

    assert matching.match_candidates([item], [client]) == []


def test_standalone_word_is_a_match():
    """Control for the compound-noun case: the same client name as a standalone
    word (surrounded by non-word characters) does match."""
    client = FakeClient("Bahn")
    item = _item("Die Bahn erhöht zum Fahrplanwechsel die Preise", "https://x.de/bahn")

    candidates = matching.match_candidates([item], [client])

    assert [c.client for c in candidates] == [client]


def test_match_is_case_insensitive():
    client = FakeClient("Beispiel AG")
    item = _item("BEISPIEL AG meldet Zahlen", "https://x.de/z")

    assert len(matching.match_candidates([item], [client])) == 1


def test_alias_hit_produces_a_candidate():
    """A client is matched on an alias, not just its canonical name."""
    client = FakeClient("Deutsche Lufthansa AG", aliases=["Lufthansa", "LH"])
    item = _item(
        "Lufthansa streicht wegen Streik hunderte Flüge",
        "https://x.de/lh",
    )

    candidates = matching.match_candidates([item], [client])

    assert candidates == [Candidate(item=item, client=client)]


def test_name_matches_without_its_legal_form():
    """Regression from live use: a mandate looked unmonitored for weeks.

    The client was entered as its register name, the press writes the brand, and
    the matcher searches whole phrases — so all seven items Google returned about
    it were discarded. The legal-form-free variant closes that (company_names).
    """
    client = FakeClient("IB-7 Beauty Tech GmbH")
    item = _item(
        "IB-7 Beauty Tech: Weltweit erste KI-Hautpflege",
        "https://cash.at/ib7",
    )

    assert matching.match_candidates([item], [client]) == [
        Candidate(item=item, client=client)
    ]


def test_the_entered_name_still_matches_when_the_form_is_spelled_out():
    """Dropping the legal form adds a variant, it never replaces the original."""
    client = FakeClient("IB-7 Beauty Tech GmbH")
    item = _item(
        "Übernahme: IB-7 Beauty Tech GmbH wechselt den Eigentümer",
        "https://x.de/ib7",
    )

    assert len(matching.match_candidates([item], [client])) == 1


def test_stripping_does_not_widen_a_name_into_a_bare_brand_word():
    """The variant is the name minus its form — not its first token.

    "Deutsche Bank AG" must not start matching every "Deutsche …" headline; only
    the trailing form comes off.
    """
    client = FakeClient("Deutsche Bank AG")
    item = _item("Deutsche Post erhöht das Porto", "https://x.de/post")

    assert matching.match_candidates([item], [client]) == []


def test_a_name_in_the_summary_produces_a_candidate():
    """The feed summary is searched, not only the headline.

    This test used to claim it proved a *keyword* hit. It never did: the summary
    it uses also names the company, so the assertion held through the name and
    would have held with the keyword removed.
    """
    client = FakeClient("Muster GmbH")
    item = _item(
        "Quartalszahlen veröffentlicht",
        "https://x.de/q",
        summary="Die Muster GmbH meldet einen Rekordgewinn im zweiten Quartal.",
    )

    assert len(matching.match_candidates([item], [client])) == 1


def test_a_topic_term_alone_is_not_evidence_the_article_is_about_the_client():
    """``keywords`` is the topic list, and this filter answers a different
    question: is this article *about* this company.

    Every other reader in the package already treats the field as topics —
    advisor, angles, assets and outreach render it into their prompts as
    "Themen:", rivals as "Beobachtete Themen", gnews builds the radar's searches
    from it. This matcher was the one place a topic counted as identity.

    Measured in production before the change: Arrakis.finance carried "Crypto,
    Krypto, DEX, Fintech, Blockchain, Exchange", so every crypto article became a
    candidate for coverage of Arrakis — 1666 analyses, zero of them relevant, one
    paid model call each. Freedom24 ran at 14% on "Aktien", "Börse", "ETFs".
    """
    client = FakeClient("Arrakis.finance", keywords=["Blockchain", "Fintech"])
    item = _item(
        "Blockchain-Regulierung: UN richtet Expertengruppe ein",
        "https://x.de/un",
        summary="Ein Gremium soll Fintech-Regeln harmonisieren.",
    )

    assert matching.match_candidates([item], [client]) == []


def test_an_identity_term_in_the_aliases_still_matches():
    """Where the identity terms belong, and where the ones filed under keywords
    were moved — "Freedom Holding Corp", "FRHC", "Timur Turlov" and the rest."""
    client = FakeClient("Freedom24", aliases=["Freedom Holding Corp", "FRHC"])
    item = _item("FRHC meldet Quartalszahlen", "https://x.de/frhc")

    assert len(matching.match_candidates([item], [client])) == 1


def test_multi_word_term_rejects_trailing_compound():
    """A multi-word term ("Deutsche Bank") is a hit as whole words but not when the
    last token runs into a longer word ("Bankfiliale")."""
    client = FakeClient("Deutsche Bank")
    hit = _item("Deutsche Bank hebt Prognose an", "https://x.de/db1")
    miss = _item("Neue Deutsche Bankfiliale eröffnet in Kiel", "https://x.de/db2")

    matched_links = {c.item.link for c in matching.match_candidates([hit, miss], [client])}

    assert matched_links == {"https://x.de/db1"}


def test_item_matching_two_clients_yields_two_candidates():
    """The same story can be about two portfolio companies; it produces one
    candidate per client (NP-06 then stores one article with two analyses)."""
    beispiel = FakeClient("Beispiel AG")
    muster = FakeClient("Muster GmbH")
    item = _item(
        "Beispiel AG und Muster GmbH gründen Joint Venture",
        "https://x.de/jv",
    )

    candidates = matching.match_candidates([item], [beispiel, muster])

    assert len(candidates) == 2
    assert {c.client for c in candidates} == {beispiel, muster}
    # One article object, shared across both candidate pairs — never duplicated.
    assert candidates[0].item is candidates[1].item


def test_no_mention_yields_no_candidate():
    client = FakeClient("Beispiel AG")
    item = _item("Ganz anderes Thema ohne Bezug", "https://x.de/none")

    assert matching.match_candidates([item], [client]) == []


def test_client_with_no_terms_is_skipped():
    """A client whose name/aliases/keywords are all blank never matches anything
    (and does not blow up compiling an empty pattern)."""
    client = FakeClient("   ", aliases=["", "  "], keywords=[])
    item = _item("Irgendeine Nachricht", "https://x.de/x")

    assert matching.match_candidates([item], [client]) == []


def test_punctuated_company_name_matches_literally():
    """A name with regex-significant punctuation ("E.ON") is matched literally, and
    the dot is not treated as a regex wildcard (so it does not match "EXON")."""
    client = FakeClient("E.ON")
    hit = _item("E.ON baut Netz aus", "https://x.de/eon")
    not_hit = _item("EXON meldet Zahlen", "https://x.de/exon")

    matched = {c.item.link for c in matching.match_candidates([hit, not_hit], [client])}

    assert matched == {"https://x.de/eon"}


# --- deduplicate: URL duplicates -----------------------------------------------


def test_url_already_stored_is_dropped():
    """An item whose link is already stored (passed via known_urls) is dropped."""
    item = _item("Neue Meldung", "https://x.de/stored")

    kept = matching.deduplicate([item], known_urls={"https://x.de/stored"})

    assert kept == []


def test_duplicate_url_within_batch_collapses_to_one():
    """Two items with the same link in one batch collapse to a single stored copy."""
    a = _item("Meldung", "https://x.de/dup", source="A", when=_WHEN)
    b = _item("Meldung", "https://x.de/dup", source="B", when=_WHEN + dt.timedelta(hours=2))

    kept = matching.deduplicate([a, b])

    assert len(kept) == 1
    assert kept[0].link == "https://x.de/dup"


def test_linkless_item_is_dropped():
    """An item with no link can't be stored (articles.url is UNIQUE/NOT NULL) and can't
    be deduplicated across runs, so it is dropped here rather than sinking the whole
    persist batch on a url="" collision (which would fail the run and break idempotency)."""
    a = _item("Eine ganz normale Nachricht ohne Link", "")
    b = _item("Noch eine andere Nachricht ohne Link", "")

    assert matching.deduplicate([a, b]) == []

    # A distinct linked item alongside a linkless one still survives.
    linked = _item("Verlinkte Meldung", "https://x.de/ok")
    kept = matching.deduplicate([a, linked])
    assert [i.link for i in kept] == ["https://x.de/ok"]


def test_distinct_urls_are_both_kept():
    a = _item("Erste ganz andere Meldung", "https://x.de/1")
    b = _item("Zweite ganz andere Meldung", "https://x.de/2")

    kept = matching.deduplicate([a, b])

    assert {i.link for i in kept} == {"https://x.de/1", "https://x.de/2"}


# --- deduplicate: near-duplicate title collapse --------------------------------


def test_near_duplicate_wire_copy_collapses_across_outlets():
    """dpa wire copy republished across outlets shares a headline but not a URL and
    carries a different outlet byline; it collapses to one stored article."""
    spiegel = _item(
        "Muster GmbH meldet Rekordgewinn im zweiten Quartal — SPIEGEL ONLINE",
        "https://spiegel.de/muster",
        source="SPIEGEL ONLINE",
        when=_WHEN + dt.timedelta(hours=3),
    )
    handelsblatt = _item(
        "Muster GmbH meldet Rekordgewinn im zweiten Quartal | Handelsblatt",
        "https://handelsblatt.de/muster",
        source="Handelsblatt",
        when=_WHEN,
    )

    kept = matching.deduplicate([spiegel, handelsblatt])

    assert len(kept) == 1
    # Retained copy is deterministic: the earliest published_at (Handelsblatt).
    assert kept[0].source == "Handelsblatt"


def test_retained_copy_tie_breaks_on_source_name():
    """Same headline, identical published_at → the retained copy is the one with the
    alphabetically-first source, so re-runs are stable."""
    later_source = _item(
        "Gleiche Schlagzeile über die Beispiel AG | Welt",
        "https://welt.de/x",
        source="Welt",
        when=_WHEN,
    )
    earlier_source = _item(
        "Gleiche Schlagzeile über die Beispiel AG — n-tv",
        "https://n-tv.de/x",
        source="n-tv",
        when=_WHEN,
    )

    kept = matching.deduplicate([later_source, earlier_source])

    assert len(kept) == 1
    assert kept[0].source == "Welt"  # "Welt" < "n-tv" (uppercase sorts first)


def test_stored_title_hash_drops_a_later_republish():
    """A wire story stored on a previous run (its title_hash in known_title_hashes)
    is not stored again when a different outlet republishes it the next day."""
    republished = _item(
        "Muster GmbH meldet Rekordgewinn im zweiten Quartal | Zeit Online",
        "https://zeit.de/muster",
        source="Zeit Online",
    )
    stored_hash = matching.title_hash(
        "Muster GmbH meldet Rekordgewinn im zweiten Quartal - manager magazin",
        "manager magazin",
    )

    kept = matching.deduplicate([republished], known_title_hashes={stored_hash})

    assert kept == []


def test_short_en_dash_clause_does_not_collapse_distinct_stories():
    """An en-dash is the German Gedankenstrich used mid-headline, not just an outlet
    byline. Two distinct same-company stories separated by a short en-dash clause must
    stay two articles — collapsing them would drop a real story (the costly dedup
    error). The byline tail matches neither item's source, so nothing is stripped."""
    absatz = _item(
        "Mercedes – Absatz bricht ein",
        "https://n-tv.de/mercedes-absatz",
        source="n-tv",
        when=_WHEN,
    )
    chef = _item(
        "Mercedes – neuer Chef ernannt",
        "https://welt.de/mercedes-chef",
        source="Welt",
        when=_WHEN + dt.timedelta(hours=1),
    )

    kept = matching.deduplicate([absatz, chef])

    assert {i.link for i in kept} == {
        "https://n-tv.de/mercedes-absatz",
        "https://welt.de/mercedes-chef",
    }


def test_deduplicate_is_stable_on_reruns():
    """Feeding the survivors of one dedup pass back through a second pass (seeded
    with what the first pass would have stored) yields nothing new — the
    idempotency the daily job relies on."""
    a = _item("Beispiel AG kündigt Fusion an", "https://a.de/x", source="A")
    b = _item("Beispiel AG kündigt Fusion an | B-Zeitung", "https://b.de/x", source="B")

    first = matching.deduplicate([a, b])
    known_urls = {i.link for i in first}
    known_hashes = {matching.title_hash(i.title, i.source) for i in first}

    second = matching.deduplicate([a, b], known_urls=known_urls, known_title_hashes=known_hashes)

    assert len(first) == 1
    assert second == []


# --- normalize_title / title_hash ----------------------------------------------


def test_source_suffix_removed_before_hashing():
    """Two headlines that differ only by their outlet byline hash the same."""
    a = matching.title_hash("Neue Fabrik für Beispiel AG — SPIEGEL ONLINE", "SPIEGEL ONLINE")
    b = matching.title_hash("Neue Fabrik für Beispiel AG | Handelsblatt", "Handelsblatt")

    assert a == b


def test_long_dash_clause_is_not_mistaken_for_a_byline():
    """A genuine descriptive dash-clause (longer than a source byline) is kept, so
    two different stories are not wrongly collapsed."""
    a = matching.normalize_title("Berlin – die Lage nach einer langen politischen Nacht")
    b = matching.normalize_title("Berlin – der Streit um den Haushalt geht weiter")

    assert a != b


def test_normalize_strips_punctuation_and_whitespace():
    a = matching.normalize_title("Beispiel  AG:  Rekord!  ")
    b = matching.normalize_title("beispiel ag rekord")

    assert a == b


# --- QA regression tests: dedup must never drop a real, distinct story ----------


def test_symbol_only_titles_never_collapse():
    """A title with no alphanumerics normalizes to "" and shares one hash with every
    other such title. Collapsing on it would drop distinct stories, and a persisted
    empty hash would black-hole every future symbol-only item. Both must fall back to
    URL-only dedup."""
    a = _item("📈", "https://a.de/1", source="A", when=_WHEN)
    b = _item("⚽", "https://b.de/2", source="B", when=_WHEN)

    kept = matching.deduplicate([a, b])
    assert {i.link for i in kept} == {"https://a.de/1", "https://b.de/2"}

    # A stored empty-title hash must not suppress a later, distinct symbol-only item.
    seeded = {matching.title_hash("🔴")}
    survived = matching.deduplicate(
        [_item("📷", "https://c.de/3", source="C")], known_title_hashes=seeded
    )
    assert [i.link for i in survived] == ["https://c.de/3"]


def test_generic_short_headline_from_two_companies_kept_apart():
    """Two different firms publishing the identical short generic PR headline are not
    wire duplicates. A too-thin title must not hash-collapse, or one client loses the
    story entirely."""
    a = _item(
        "Quartalszahlen veröffentlicht", "https://a-ag.de/pr", source="A-AG", when=_WHEN
    )
    b = _item(
        "Quartalszahlen veröffentlicht",
        "https://b-gmbh.de/pr",
        source="B-GmbH",
        when=_WHEN + dt.timedelta(hours=1),
    )

    kept = matching.deduplicate([a, b])

    assert {i.link for i in kept} == {"https://a-ag.de/pr", "https://b-gmbh.de/pr"}


def test_middot_and_pipe_rubric_prefix_stays_distinct():
    """A pipe/middot after a short *rubric* head ("Formel 1 ·", "Liveticker |") is a
    section label, not an outlet byline; the tail is the real story. Distinct stories
    sharing only the rubric must not normalize to the same hash."""
    assert matching.normalize_title(
        "Formel 1 · Verstappen siegt"
    ) != matching.normalize_title("Formel 1 · Hamilton crasht")
    assert matching.normalize_title(
        "Liveticker | Bayern gewinnt"
    ) != matching.normalize_title("Liveticker | Dortmund verliert")


def test_esszett_client_matches_ss_headline():
    """Case-folding both sides applies the German ß→ss fold, so a client "Straße"
    matches a headline rendered "STRASSE" — a recall gap re.IGNORECASE leaves open."""
    client = FakeClient("Straße")
    item = _item("STRASSE meldet Zahlen", "https://x.de/ss")

    assert len(matching.match_candidates([item], [client])) == 1


def test_multi_word_term_matches_hyphenated_headline():
    """Recall-first: a multi-word client "Deutsche Bank" also matches a hyphenated
    "Deutsche-Bank", where the inter-word gap is punctuation, not whitespace."""
    client = FakeClient("Deutsche Bank")
    item = _item("Deutsche-Bank stellt neue Strategie vor", "https://x.de/db")

    assert len(matching.match_candidates([item], [client])) == 1


# --- URL canonicalization for dedup -------------------------------------------


def test_canonical_url_collapses_referral_spellings_of_one_page():
    """Tracking params, scheme, www., port, trailing slash and fragment are all
    referral noise, not article identity."""
    canonical_url = matching.canonical_url

    target = canonical_url("https://x.de/story")
    for variant in (
        "https://x.de/story?utm_source=rss&utm_medium=feed",
        "http://x.de/story",
        "https://www.x.de/story",
        "http://x.de:80/story",
        "https://x.de:443/story",
        "https://x.de/story/",
        "https://x.de/story#top",
        "https://X.DE/story",
        "https://x.de/story?ref=newsletter&fbclid=abc",
    ):
        assert canonical_url(variant) == target, variant


def test_canonical_url_preserves_real_query_params_and_paths():
    """Identity must not over-collapse: a query id, a path, and a host each
    distinguish genuinely different articles."""
    canonical_url = matching.canonical_url

    assert canonical_url("https://x.de/a?id=1") != canonical_url("https://x.de/a?id=2")
    assert canonical_url("https://x.de/p1") != canonical_url("https://x.de/p2")
    assert canonical_url("https://a.de/x") != canonical_url("https://b.de/x")
    # Order of real params is not identity.
    assert canonical_url("https://x.de/a?b=2&a=1") == canonical_url("https://x.de/a?a=1&b=2")
    # A non-URL has no identity to normalize; it compares as itself.
    assert canonical_url("notaurl") == "notaurl"
    assert canonical_url("") == ""


def test_deduplicate_collapses_tracking_variant_of_a_thin_title():
    """The title-hash gate skips short headlines, so URL identity is the only
    axis left — it must survive a tracking parameter."""
    items = [
        _item("Siemens Rückruf", "https://x.de/c"),
        _item("Siemens Rückruf", "https://x.de/c?utm_source=rss"),
    ]
    assert len(matching.deduplicate(items)) == 1


def test_deduplicate_matches_stored_url_against_tracking_variant():
    """known_urls arrives as raw stored URLs; a re-fetch carrying a tracking
    parameter must still be recognised as already stored."""
    kept = matching.deduplicate(
        [_item("Siemens Rückruf", "https://x.de/h?utm_medium=rss")],
        known_urls={"https://www.x.de/h"},
    )
    assert kept == []
