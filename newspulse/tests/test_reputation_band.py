"""The band on Heute: what it says, and the mornings it says almost nothing.

Interface-level, through FastAPI's ``TestClient`` against a seeded in-memory
database. The ``get_db`` dependency is overridden so no real database file and no
sweep are involved; nothing here reaches a model or the network.

Two of these tests are the acceptance criteria that are easiest to lose in a
refactor and hardest to notice afterwards:

* the morning on which every mandate is quiet, when the band has to be one line
  and *no* empty tiles — a band that renders ten grey placeholders on a normal
  Tuesday is the decoration this feature was written against;
* the morning before the sweep has ever run, when there is no band at all
  rather than a line claiming a calm nobody measured.

The third property is that colour never carries a statement: every rung, every
direction and the deviation are asserted as *text* in the rendered body, in both
languages, because a class name is not something a reader reads.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, i18n
from newspulse.models import (
    Article,
    Base,
    Client,
    Crisis,
    ReputationReading,
    ReputationState,
)
from newspulse.web.app import create_app, get_db

#: The band reads each mandate's newest stored reading, so the fixtures date
#: themselves off the local day rather than off a hardcoded one — the stamp the
#: band renders is the reading's day, and a fixed date would drift out of the
#: window the page is looked at through.
_TODAY = dt.datetime.now(config.local_zone()).date()


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    app = create_app()

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _mandate(session, name: str) -> Client:
    obj = Client(name=name)
    session.add(obj)
    session.flush()
    return obj


def _reading(
    session,
    mandate: Client,
    *,
    day: dt.date | None = None,
    state: ReputationState = ReputationState.RUHIG,
    points: int = 0,
    outlets: int = 0,
    articles: int = 0,
    negative: int = 0,
    national: bool = False,
    named: bool = False,
) -> ReputationReading:
    row = ReputationReading(
        client_id=mandate.id,
        day=day or _TODAY,
        state=state,
        outlets=outlets,
        national=national,
        articles=articles,
        negative=negative,
        named=named,
        points=points,
        computed_at=dt.datetime.now(dt.UTC),
    )
    session.add(row)
    return row


def _seed_article(session) -> Article:
    """A trigger article for a crisis. Its content is irrelevant here — the band
    reads the crisis row's existence and never the coverage behind it."""
    when = dt.datetime.now(dt.UTC)
    article = Article(
        title="Verbraucherzentrale mahnt ab",
        url=f"https://faz.example.de/{session.info.get('n', 0)}-a1",
        source="FAZ",
        published_at=when,
        fetched_at=when,
        title_hash="h1",
    )
    session.add(article)
    session.flush()
    return article


def _series(session, mandate: Client, points: list[int]) -> None:
    """Readings for consecutive days ending yesterday, oldest first.

    The band's direction and its "unusual for this mandate" line are both read
    off a *series*, so a test about either has to seed one — a single row can
    only ever produce "stabil" and no deviation, which is exactly the answer that
    would hide a broken comparison.
    """
    start = _TODAY - dt.timedelta(days=len(points))
    for offset, value in enumerate(points):
        _reading(
            session,
            mandate,
            day=start + dt.timedelta(days=offset),
            state=(
                ReputationState.BEOBACHTUNG if value else ReputationState.RUHIG
            ),
            points=value,
            negative=1 if value else 0,
            articles=1 if value else 0,
        )


def _band_of(body: str) -> str:
    """Just the band's markup out of a rendered page.

    Every mandate's name appears in the filter strip above the band as well, so
    an assertion about what the band says has to be an assertion about the band
    — otherwise "the worst mandate comes first" quietly becomes "the filter
    strip is alphabetical", which is true and is not the criterion.
    """
    start = body.index('<section class="band"')
    return body[start : body.index("</section>", start)]


# --- The quiet morning, which is most of them ------------------------------------


def test_a_portfolio_that_is_entirely_quiet_is_one_line_and_no_tiles(
    factory, client
):
    """DEC-1 option B's whole bargain, and the criterion it is easiest to break.

    Three quiet mandates: the band names the count and renders no tile at all. A
    row of grey placeholders would cost the top of the screen every morning for
    the statement that nothing is happening — which is how a band stops being
    read by the fortnight it matters.
    """
    with factory() as s:
        for name in ("Alpha AG", "Beta AG", "Gamma AG"):
            _reading(s, _mandate(s, name), articles=2)
        s.commit()

    body = client.get("/today").text

    assert "3 Mandanten ruhig" in body
    assert 'class="band__tile' not in body
    assert 'class="band__row"' not in body


def test_the_quiet_line_names_the_mandates_nobody_wrote_about(factory, client):
    """"Ruhig" and "nothing was published" are different mornings.

    A mandate with no coverage in the window is quiet and *not* unknown, and the
    band has to be able to say which of the two a number stands for — otherwise
    a portfolio whose feeds have been dark for a week reads exactly like a
    portfolio having a good week.
    """
    with factory() as s:
        _reading(s, _mandate(s, "Alpha AG"), articles=4)
        _reading(s, _mandate(s, "Beta AG"), articles=0)
        _reading(s, _mandate(s, "Gamma AG"), articles=0)
        s.commit()

    body = client.get("/today").text

    assert "3 Mandanten ruhig" in body
    assert "2 davon ohne Berichterstattung" in body


def test_a_single_quiet_mandate_is_named_in_the_singular(factory, client):
    with factory() as s:
        _reading(s, _mandate(s, "Alpha AG"), articles=1)
        s.commit()

    body = client.get("/today").text

    assert "1 Mandant ruhig" in body
    assert "Mandanten ruhig" not in body


def test_no_reading_at_all_renders_no_band_rather_than_a_claim_of_calm(
    factory, client
):
    """Before the first sweep there is nothing to say, and the band says nothing.

    "Alle Mandanten ruhig" for a portfolio nobody has ever measured would be the
    worst sentence on the page: it is indistinguishable from a working morning
    and it is produced by a broken one.
    """
    with factory() as s:
        _mandate(s, "Alpha AG")
        _mandate(s, "Beta AG")
        s.commit()

    body = client.get("/today").text

    assert '<section class="band"' not in body
    assert 'class="band__foot"' not in body


# --- The morning something is happening ------------------------------------------


def test_a_mandate_above_the_lowest_rung_gets_a_tile_with_its_four_inputs(
    factory, client
):
    """The rung, and the numbers it was counted from, beside it.

    DEC-2 option A is only worth anything if the counts are on the page: a
    consultant asked "why is this Risiko" has to get the four numbers in the hour
    they ask, not a page that shows a colour and keeps the arithmetic in a table.
    """
    with factory() as s:
        loud = _mandate(s, "Alpha AG")
        _reading(
            s, loud,
            state=ReputationState.RISIKO, points=5,
            outlets=3, articles=6, negative=4, national=True, named=True,
        )
        _reading(s, _mandate(s, "Beta AG"), articles=1)
        s.commit()

    body = client.get("/today").text

    assert "Alpha AG" in body
    assert "band__tile--risiko" in body
    assert "3 Medien" in body
    assert "4/6 negativ" in body
    assert "überregional" in body
    assert "namentlich genannt" in body
    # And the rest of the portfolio is still the one line behind it.
    assert "1 Mandant ruhig" in body


def test_the_rung_is_a_word_and_not_only_a_colour(factory, client):
    """"Farbe allein trägt nie eine Aussage".

    The class is asserted *and* the word, because only one of the two is
    something a reader reads — and a refactor that keeps the palette while
    dropping the label would pass a test that checked the class alone.
    """
    with factory() as s:
        _reading(
            s, _mandate(s, "Alpha AG"),
            state=ReputationState.KRISE, points=7,
            outlets=5, articles=5, negative=5, national=True, named=True,
        )
        s.commit()

    body = client.get("/today").text

    assert "band__tile--krise" in body
    assert ">Krise<" in body


def test_the_direction_is_shown_as_a_word(factory, client):
    """Seven readings behind today, the newest above the median of the six.

    Counted by hand: the series is 0, 0, 1, 1, 1, 1 behind a newest of 5, so the
    median behind it is 1 and the direction is "steigend" — and the band prints
    the word rather than an arrow, which would be a second thing to learn.
    """
    with factory() as s:
        mandate = _mandate(s, "Alpha AG")
        _series(s, mandate, [0, 0, 1, 1, 1, 1])
        _reading(
            s, mandate,
            state=ReputationState.RISIKO, points=5,
            outlets=2, articles=3, negative=3, named=True,
        )
        s.commit()

    body = client.get("/today").text

    assert "Richtung:" in body
    assert "<strong>steigend</strong>" in body


def test_a_reading_above_the_mandates_own_median_is_named_in_the_line(
    factory, client
):
    """The deviation is a sentence, not a mark.

    Ten prior readings at one point each and a newest at five: five is above a
    median of one, so today is unusual *for this mandate*. The same five would be
    ordinary for a mandate whose median is five, and the band says which of the
    two it is rather than leaving the reader to guess at a threshold.
    """
    with factory() as s:
        mandate = _mandate(s, "Alpha AG")
        _series(s, mandate, [1] * 10)
        _reading(
            s, mandate,
            state=ReputationState.RISIKO, points=5,
            outlets=2, articles=2, negative=2, named=True,
        )
        s.commit()

    body = client.get("/today").text

    assert "über dem eigenen Median der letzten 30 Ablesungen" in body


def test_a_mandate_without_a_baseline_makes_no_claim_about_being_unusual(
    factory, client
):
    """Two readings are not a baseline, and the band does not pretend otherwise.

    Without this, every mandate would carry "ungewöhnlich für dieses Mandat" in
    its first week and the sentence would mean nothing by the second.
    """
    with factory() as s:
        mandate = _mandate(s, "Alpha AG")
        _series(s, mandate, [0, 1])
        _reading(
            s, mandate,
            state=ReputationState.RISIKO, points=5,
            outlets=2, articles=2, negative=2, named=True,
        )
        s.commit()

    body = client.get("/today").text

    assert "band__tile--risiko" in body
    assert "über dem eigenen Median" not in body


def test_a_mandate_with_no_coverage_says_so_on_its_tile(factory, client):
    """A tile can be quiet in its counts and still not be on the lowest rung —
    an open crisis holds a mandate at Krise with nothing published today, and the
    tile has to read as "nothing lay" rather than as three empty numbers."""
    with factory() as s:
        _reading(
            s, _mandate(s, "Alpha AG"),
            state=ReputationState.KRISE, points=0,
        )
        s.commit()

    body = client.get("/today").text

    assert "keine Berichterstattung im Fenster" in body
    assert "0/0 negativ" not in body


def test_the_tiles_are_ordered_worst_first(factory, client):
    """The band is read top-left first, so the worst mandate is there."""
    with factory() as s:
        _reading(
            s, _mandate(s, "Alpha AG"),
            state=ReputationState.BEOBACHTUNG, points=2,
            outlets=1, articles=2, negative=1,
        )
        _reading(
            s, _mandate(s, "Beta AG"),
            state=ReputationState.KRISE, points=7,
            outlets=5, articles=5, negative=5, national=True, named=True,
        )
        s.commit()

    # Sliced to the band: every mandate is also named in the filter strip above
    # it, alphabetically, so a search over the whole page would answer a question
    # about the filter strip's order rather than about the band's.
    body = _band_of(client.get("/today").text)

    assert body.index("Beta AG") < body.index("Alpha AG")


def test_the_band_follows_the_client_filter(factory, client):
    """A page filtered to one mandate is a page about one mandate.

    A "zwei Mandanten ruhig" beside one mandate's coverage counts a mandate that
    is not on the page, which is a sentence about a portfolio the reader has just
    said they are not looking at.
    """
    with factory() as s:
        alpha = _mandate(s, "Alpha AG")
        _reading(
            s, alpha,
            state=ReputationState.RISIKO, points=5,
            outlets=2, articles=2, negative=2, named=True,
        )
        _reading(s, _mandate(s, "Beta AG"), articles=1)
        s.commit()

    body = _band_of(client.get(f"/today?client={alpha.id}").text)

    assert "band__tile--risiko" in body
    assert "ruhig" not in body


def test_the_band_stands_above_the_days_coverage(factory, client):
    """"über der Tagesberichterstattung", literally: the band is read first."""
    with factory() as s:
        _reading(
            s, _mandate(s, "Alpha AG"),
            state=ReputationState.RISIKO, points=5,
            outlets=2, articles=2, negative=2, named=True,
        )
        s.commit()

    body = client.get("/today").text

    assert body.index('class="band"') < body.index('class="cols"')


# --- Both languages ---------------------------------------------------------------


def test_every_visible_string_on_the_band_exists_in_english(factory, client):
    """The failure a two-language interface actually has is a *mixed* page.

    A band whose tiles keep their German rung beside an English "Trend" reads as
    broken in a way a wholly German one does not, so the whole line is checked at
    once rather than key by key.
    """
    with factory() as s:
        mandate = _mandate(s, "Alpha AG")
        _series(s, mandate, [1] * 10)
        _reading(
            s, mandate,
            state=ReputationState.RISIKO, points=5,
            outlets=3, articles=6, negative=4, national=True, named=True,
        )
        _reading(s, _mandate(s, "Beta AG"), articles=0)
        s.commit()

    client.cookies.set(i18n.COOKIE_NAME, "en")
    body = _band_of(client.get("/today").text)

    for english in (
        "Risk",
        "Trend:",
        "national reach",
        "named",
        "1 client quiet",
        "of them with no coverage",
        "above its own median of the last 30 readings",
        "Reading of",
    ):
        assert english in body, english
    for german in (
        "Risiko",
        "Richtung:",
        "überregional",
        "namentlich genannt",
        "Mandant ruhig",
        "davon ohne Berichterstattung",
        "Ablesung vom",
    ):
        assert german not in body, german


def test_every_rung_and_every_direction_has_an_english_word(factory, client):
    """The exhaustive guard behind the test above it.

    That one renders one rung and one direction; this one walks both value sets,
    because the way this criterion actually breaks is a rung added in a later
    story whose translation nobody remembered — and the page it breaks on would
    then be half English and half German, which reads as a defect rather than as
    a missing string.

    Presence in the table, not "the English differs": ``issue`` is the same word
    in both languages and is nonetheless translated, while an untranslated key
    silently degrades to German and would pass any test written the other way.
    """
    from newspulse import reputation
    from newspulse.models import ReputationState as State

    for value in [state.value for state in State] + [
        direction.value for direction in reputation.Direction
    ]:
        assert value in i18n._EN, value


# --- The one thing the band does not read off the stored reading -----------------


def test_a_crisis_declared_after_the_sweep_shows_on_the_band_at_once(
    factory, client
):
    """The floor is a person's statement, and it is true the second it is made.

    The reading was taken at 06:10 and says ruhig; somebody declares a crisis at
    two in the afternoon. Without the floor being applied at read as well, the
    band would sit on the same screen as that mandate's own crisis card and call
    it quiet until the next morning — the most visible way this feature could
    look broken, and the one a reader would be right to distrust it for.
    """
    with factory() as s:
        mandate = _mandate(s, "Alpha AG")
        _reading(s, mandate, state=ReputationState.RUHIG, articles=1)
        article = _seed_article(s)
        s.add(
            Crisis(
                client_id=mandate.id,
                article_id=article.id,
                declared_by="lucas",
                declared_at=dt.datetime.now(dt.UTC),
                level=3,
            )
        )
        s.commit()

    body = _band_of(client.get("/today").text)

    assert "band__tile--krise" in body
    assert ">Krise<" in body
    assert "ruhig" not in body


def test_the_counts_on_the_tile_stay_the_ones_the_sweep_stored(factory, client):
    """The floor raises the rung and recomputes nothing else.

    A band that re-counted on render would move under the reader during the
    morning with no run having happened, and would disagree with the row a
    consultant is about to re-derive it from. So the tile of a mandate the floor
    just raised still carries yesterday's — this morning's — numbers.
    """
    with factory() as s:
        mandate = _mandate(s, "Alpha AG")
        _reading(
            s, mandate,
            state=ReputationState.BEOBACHTUNG, points=2,
            outlets=1, articles=4, negative=1,
        )
        article = _seed_article(s)
        s.add(
            Crisis(
                client_id=mandate.id,
                article_id=article.id,
                declared_by="lucas",
                declared_at=dt.datetime.now(dt.UTC),
                level=3,
            )
        )
        s.commit()

    body = _band_of(client.get("/today").text)

    assert "band__tile--krise" in body
    assert "1 Medien" in body
    assert "1/4 negativ" in body


def test_a_closed_crisis_does_not_raise_the_band(factory, client):
    """A stood-down crisis is a finished document, not a standing state."""
    with factory() as s:
        mandate = _mandate(s, "Alpha AG")
        _reading(s, mandate, state=ReputationState.RUHIG, articles=1)
        article = _seed_article(s)
        s.add(
            Crisis(
                client_id=mandate.id,
                article_id=article.id,
                declared_by="lucas",
                declared_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
                closed_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
                close_reason="vorbei",
                level=3,
            )
        )
        s.commit()

    body = _band_of(client.get("/today").text)

    assert "1 Mandant ruhig" in body
    assert "band__tile" not in body
