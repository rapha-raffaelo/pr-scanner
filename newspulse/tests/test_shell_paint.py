"""Der Anstrich aus der Vorlage (GEL-03, DEC-4): the shell, repainted.

The story is a coat of paint over a tool Lucas opens every morning, so the
load-bearing tests here are the ones that would catch a repaint that broke
something rather than the ones that admire the new colour:

* every page that existed before still answers 200 — Heute, Portfolio, Archiv,
  Kontakte and every page of a mandate's workspace;
* every href in the new top bar resolves; a bar with a dead control in it is
  the one thing DEC-4 says not to build, so the test calls each one;
* the tab strip keeps its names, its order and its targets, and only gains a
  symbol and a new selected state;
* the accent is one line in ``app.css`` and no rule spells it out beside that
  line, which is the whole reason DEC-4 chose tokens over twenty rules.

``nav_crumbs`` is tested directly, against a stub carrying the three attributes
it reads, rather than through a rendered page: it is a pure function of a path
plus one row, and a page render is a slower way to ask the same question.

Nothing here reaches a model and nothing reaches the network.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Base, Client
from newspulse.web import navigation
from newspulse.web.app import create_app, get_db

_CSS = (
    Path(navigation.__file__).resolve().parent / "static" / "app.css"
)

#: The tab strip, in the order ``_client_tabs.html`` prints it: the label, the
#: href suffix behind it, and the symbol drawn ahead of it. Written out here
#: rather than scraped from the template, because the point of the test is that
#: the repaint did not quietly reorder or rename anything — a list read back
#: out of the thing under test would agree with any change at all.
#:
#: "Krise" is absent: it appears only for a mandate that ever declared one
#: (UHR-03), and this file's fixture never does.
_TABS: tuple[tuple[str, str, str], ...] = (
    ("Heute", "/heute", "ico-heute"),
    # Renamed on main while this branch was open: the page under it is headed
    # "Impulse", and a tab that names its page is easier to find.
    ("Impulse", "/advice", "ico-texte"),
    ("Issues", "/issues", "ico-issues"),
    ("Plan", "/plan", "ico-plan"),
    ("Wettbewerb", "/wettbewerb", "ico-wettbewerb"),
    ("Archiv", "", "ico-archiv"),
    ("Kickoff", "/kickoff", "ico-kickoff"),
    ("Profil", "/profil", "ico-profil"),
    ("Guide", "/guide", "ico-guide"),
    ("Marktumfeld", "/market", "ico-markt"),
    ("KI-Sichtbarkeit", "/ki", "ico-ki"),
)

#: The pages that are not a mandate's. The story names four; ``/settings`` is
#: here too because the sidebar's footer links to it and the repaint touched the
#: sidebar.
_PORTFOLIO_PAGES: tuple[str, ...] = ("/", "/today", "/archive", "/contacts", "/settings")

#: The mandate pages that are not in the tab strip but are reachable from one.
_EXTRA_CLIENT_PAGES: tuple[str, ...] = ("/map", "/berichte", "/pressespiegel")


# --- Fixtures ---------------------------------------------------------------------


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as open_session:
        yield open_session


@pytest.fixture
def web(factory):
    app = create_app()

    def _override():
        open_session = factory()
        try:
            yield open_session
        finally:
            open_session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture
def mandate(session) -> Client:
    client = Client(name="Solaris AG", aliases=["Solaris"], industry="Solarenergie")
    session.add(client)
    session.commit()
    return client


# --- Helpers ----------------------------------------------------------------------


def _topbar(html: str) -> str:
    """The top bar's own markup, cut out of a rendered page.

    Cut at the toolbar that follows it rather than at a closing tag: the bar
    holds a nav, a form and a wrapper, so counting ``</div>`` would end the
    slice three elements early and the test would pass by looking at less.
    """
    assert '<div class="topbar">' in html, "no top bar on the page"
    after = html.split('<div class="topbar">', 1)[1]
    assert '<header class="tbar"' in after, "the toolbar no longer follows the bar"
    return after.split('<header class="tbar"', 1)[0]


def _rules_only(css: str) -> str:
    """The stylesheet with its comments taken out.

    The accent's decimal triple is unreadable, so the block that declares it
    names the hex in prose. That is documentation, not a second definition, and
    the count below is about definitions.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _links(markup: str) -> list[str]:
    """Every destination the markup names: hrefs and form actions alike."""
    found = re.findall(r'(?:href|action)="([^"#]+)"', markup)
    # The icon sprite is referenced by fragment and is filtered out by the
    # pattern above; anything left that is not a path is a bug in the bar.
    return [url for url in found if url.startswith("/")]


# --- Every page still loads -------------------------------------------------------


@pytest.mark.parametrize("path", _PORTFOLIO_PAGES)
def test_a_portfolio_page_still_answers_200_after_the_repaint(web, mandate, path):
    assert web.get(path).status_code == 200


@pytest.mark.parametrize("suffix", [suffix for _, suffix, _ in _TABS] + list(_EXTRA_CLIENT_PAGES))
def test_a_mandate_page_still_answers_200_after_the_repaint(web, mandate, suffix):
    assert web.get(f"/client/{mandate.id}{suffix}").status_code == 200


# --- The bar leads nowhere dead ---------------------------------------------------


def test_no_destination_in_the_top_bar_answers_404(web, mandate):
    """DEC-4's one prohibition: a control up here with nothing behind it.

    Every href and every form action in the bar is called, on a mandate page so
    the crumb carries its second, deeper link too.
    """
    bar = _topbar(web.get(f"/client/{mandate.id}/profil").text)
    destinations = _links(bar)
    assert len(destinations) >= 3, f"expected path, search and assistant, got {destinations}"
    for url in destinations:
        assert web.get(url).status_code != 404, url


def test_the_bar_holds_no_control_without_a_destination(web, mandate):
    """A button in the bar is either a link or the form's own submit.

    The Vorlage shows more controls up here than the tool has pages for; the
    test is that none of them arrived without something behind it.
    """
    bar = _topbar(web.get("/today").text)
    buttons = re.findall(r"<button[^>]*>", bar)
    assert len(buttons) == 1, f"one submit, and it belongs to the search: {buttons}"
    assert 'type="submit"' in buttons[0]


# --- The path ---------------------------------------------------------------------


def test_the_bar_names_the_workspace_and_the_mandate(web, mandate):
    bar = _topbar(web.get(f"/client/{mandate.id}/profil").text)
    assert "Portfolio" in bar
    assert "Solaris AG" in bar
    assert f'href="/client/{mandate.id}"' in bar


def test_the_bar_names_the_workspace_alone_off_a_mandate(web, mandate):
    bar = _topbar(web.get("/archive").text)
    assert "Archiv" in bar
    assert "Solaris AG" not in bar, "no mandate is being looked at here"


def test_the_path_is_translated_but_a_mandate_name_is_not(web, mandate):
    """The crumb reuses the sidebar's own labels, so it switches with them —
    and the stored name stays the stored name."""
    web.cookies.set("newspulse_lang", "en")
    bar = _topbar(web.get(f"/client/{mandate.id}/profil").text)
    assert "Clients" in bar
    assert "Solaris AG" in bar


# --- nav_crumbs, directly ---------------------------------------------------------


def _request(path: str, db=None, **query: str):
    """The three attributes ``nav_crumbs`` reads, and nothing else."""
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        query_params=query,
        state=SimpleNamespace(db=db),
    )


@pytest.mark.parametrize(
    ("path", "area"),
    [
        ("/", "Portfolio"),
        ("/clients", "Portfolio"),
        ("/today", "Heute"),
        ("/archive", "Archiv"),
        ("/contacts", "Kontakte"),
        ("/settings", "Einstellungen"),
        ("/settings/brain/pitch", "Einstellungen"),
    ],
)
def test_a_portfolio_path_crumbs_to_its_own_workspace(path, area):
    crumbs = navigation.nav_crumbs(_request(path))
    assert crumbs.area == area
    assert crumbs.mandate is None


def test_a_mandate_path_crumbs_to_the_workspace_and_the_name(session, mandate):
    crumbs = navigation.nav_crumbs(_request(f"/client/{mandate.id}/guide", db=session))
    assert crumbs.area == "Portfolio"
    assert crumbs.area_href == "/"
    assert crumbs.mandate == "Solaris AG"
    assert crumbs.mandate_href == f"/client/{mandate.id}"


def test_the_day_of_one_mandate_carries_that_mandate(session, mandate):
    """The sidebar's mandate row lands on ``/today?client=`` (the /heute route
    redirects), and that page carries the workspace strip. The crumb has to name
    the mandate there too, or the bar contradicts the tabs under it — while the
    workspace stays "Heute", which is the row the sidebar is highlighting."""
    crumbs = navigation.nav_crumbs(_request("/today", db=session, client=str(mandate.id)))
    assert crumbs.area == "Heute"
    assert crumbs.mandate == "Solaris AG"


def test_a_client_filter_that_is_not_a_number_is_no_mandate(session):
    """``?client=`` is reader-supplied. A stray value leaves the day's own crumb
    standing rather than raising on the way to a page that renders fine."""
    crumbs = navigation.nav_crumbs(_request("/today", db=session, client="alle"))
    assert crumbs.area == "Heute"
    assert crumbs.mandate is None


@pytest.mark.parametrize("stray", ["99999999999999999999", "\u00b2", "\u2460", "-1", "0"])
def test_a_client_filter_no_row_could_carry_is_no_mandate(session, stray):
    """``?client=`` reaches the bar raw, and ``/today`` answers 200 for any
    integer — it matches the id against the roster in Python. So the crumb has
    to survive the same values: a number past what the id column holds used to
    raise OverflowError inside the template, turning a working page into a 500,
    and '\u00b2' passes ``str.isdigit()`` but not ``int()``."""
    crumbs = navigation.nav_crumbs(_request("/today", db=session, client=stray))
    assert crumbs.area == "Heute"
    assert crumbs.mandate is None


def test_the_workspace_crumb_is_the_word_the_sidebar_uses_for_that_page(web, mandate):
    """The crumb links somewhere; the sidebar names that somewhere. If the two
    disagree, clicking the crumb lands on a page highlighting another word."""
    page = web.get(f"/client/{mandate.id}/profil").text
    nav = re.search(r'<nav class="crumbs".*?</nav>', page, re.S)
    assert nav is not None
    crumb = re.search(r'<a[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', nav.group(0))
    assert crumb is not None, "a mandate page carries a workspace crumb above it"
    assert crumb.group(1) == "/"
    row = re.search(r'<a href="/" class="side__row[^"]*"[^>]*>([^<]*)</a>', page)
    assert row is not None
    assert crumb.group(2).strip() == row.group(1).strip()


def test_an_unknown_mandate_crumbs_to_the_workspace_alone(session):
    """The 404 under it says the rest; the bar does not invent a name."""
    crumbs = navigation.nav_crumbs(_request("/client/9999/guide", db=session))
    assert crumbs.area == "Portfolio"
    assert crumbs.mandate is None


def test_no_session_on_the_request_is_a_workspace_not_a_500():
    """Same rule the sidebar follows: a thinner bar beats a broken page."""
    crumbs = navigation.nav_crumbs(_request("/client/1/heute"))
    assert crumbs.area == "Portfolio"
    assert crumbs.mandate is None


# --- The search field -------------------------------------------------------------


def test_the_search_field_submits_to_the_archive_search(web, mandate):
    bar = _topbar(web.get("/today").text)
    assert 'action="/archive"' in bar
    assert 'name="q"' in bar
    assert 'method="get"' in bar


def test_a_term_typed_in_the_bar_reaches_the_archive_filter(web, mandate):
    """The field is not a new search: it hands the term to the filter the
    archive page already runs."""
    page = web.get("/archive", params={"q": "Netzentgelte"})
    assert page.status_code == 200
    assert 'value="Netzentgelte"' in page.text


def test_the_field_keeps_the_term_while_the_archive_is_the_page(web, mandate):
    bar = _topbar(web.get("/archive", params={"q": "Netzentgelte"}).text)
    assert 'value="Netzentgelte"' in bar
    elsewhere = _topbar(web.get("/today", params={"q": "Netzentgelte"}).text)
    assert 'value=""' in elsewhere, "the term belongs to the archive, not to Heute"


# --- Ask RAUTE --------------------------------------------------------------------


def test_ask_raute_opens_the_assistant_that_already_runs(web, mandate):
    """No new page and no new route: the href is this page with the drawer
    unfolded, which is how the layout has opened it all along."""
    bar = _topbar(web.get("/today").text)
    assert "Ask RAUTE" in bar
    target = next(url for url in _links(bar) if "assistant=1" in url)

    opened = web.get(target)
    assert opened.status_code == 200
    drawer = opened.text.split('id="assistant-drawer"', 1)[1].split(">", 1)[0]
    assert "hidden" not in drawer, "the drawer should be unfolded on arrival"


def test_ask_raute_keeps_the_page_it_was_pressed_on(web, mandate):
    """Pressed on a filtered archive, it must come back to that archive — a
    control that drops the reader's filters is a control that gets avoided."""
    bar = _topbar(web.get("/archive", params={"q": "Netzentgelte"}).text)
    target = next(url for url in _links(bar) if "assistant=1" in url)
    assert target.startswith("/archive?")
    assert "q=Netzentgelte" in target


def test_ask_raute_does_not_stack_up_when_it_is_pressed_twice(web, mandate):
    """Without JavaScript the link lands on a page that already carries
    ?assistant, where the bar renders the link again. It has to *set* the flag
    rather than append it, or the URL grows one &assistant=1 per press."""
    page = web.get("/archive", params={"q": "Netzentgelte", "assistant": "1"}).text
    target = next(url for url in _links(_topbar(page)) if "assistant=1" in url)
    assert target.count("assistant=1") == 1, target
    assert "q=Netzentgelte" in target, "and it still keeps the filter underneath"


# --- The tab strip ----------------------------------------------------------------


def _strip(html: str) -> str:
    assert 'class="subtabs"' in html, "no workspace strip on the page"
    return html.split('class="subtabs"', 1)[1].split("</nav>", 1)[0]


def test_the_tabs_keep_their_names_their_order_and_their_targets(web, mandate):
    """The repaint is allowed to change how a tab looks and nothing else."""
    strip = _strip(web.get(f"/client/{mandate.id}/heute").text)

    seen = [label for label in re.findall(r"</svg>([^<]+)</a>", strip)]
    assert seen == [label for label, _, _ in _TABS]

    for label, suffix, _ in _TABS:
        assert f'href="/client/{mandate.id}{suffix}"' in strip, label


def test_every_tab_carries_its_own_symbol(web, mandate):
    strip = _strip(web.get(f"/client/{mandate.id}/heute").text)
    for label, _, symbol in _TABS:
        assert f'href="#{symbol}"' in strip, f"{label} has no symbol"
    assert strip.count("subtabs__ico") == len(_TABS)


def test_the_symbols_are_drawn_once_in_the_layout(web, mandate):
    """One sprite for the whole page, referenced by <use>: eleven tabs on twelve
    pages should not cost a hundred and thirty-two copies of the geometry."""
    page = web.get(f"/client/{mandate.id}/heute").text
    assert page.count('<symbol id="ico-heute"') == 1
    assert page.count('<use href="#ico-heute">') == 1


def test_the_selected_tab_is_the_one_the_page_is(web, mandate):
    strip = _strip(web.get(f"/client/{mandate.id}/profil").text)
    active = re.findall(r'<a href="([^"]+)"[^>]*class="active"', strip)
    assert active == [f"/client/{mandate.id}/profil"]


def test_a_tab_with_no_page_behind_it_is_not_in_the_strip(web, mandate):
    """Same rule as the top bar, and the reason "Krise" is conditional: the
    strip carries no href this app does not serve."""
    strip = _strip(web.get(f"/client/{mandate.id}/heute").text)
    for url in _links(strip):
        assert web.get(url).status_code != 404, url


# --- The sidebar keeps its structure ----------------------------------------------


def test_the_sidebar_keeps_its_rows_and_only_changes_how_they_look(web, mandate):
    """DEC-4 repaints the sidebar; it does not rebuild it. The rows the tool had
    before are the rows it has now, in the same order, pointing at the same
    pages."""
    page = web.get("/today").text
    side = page.split('<aside class="side">', 1)[1].split("</aside>", 1)[0]
    rows = re.findall(r'<a href="([^"]+)" class="side__row', side)
    assert rows[:4] == ["/today", "/", "/archive", "/contacts"]
    assert f"/client/{mandate.id}/heute" in side
    assert "/settings" in side


def test_the_page_being_looked_at_is_the_selected_row(web, mandate):
    side = web.get("/today").text.split('<aside class="side">', 1)[1]
    row = next(line for line in side.splitlines() if 'href="/today"' in line)
    assert "is-on" in row
    assert 'aria-current="page"' in row


# --- The paint lives in one place -------------------------------------------------


def test_the_accent_is_one_line_and_no_module_repeats_it():
    """The point of DEC-4: swapping the accent is editing one declaration.

    A hue spelled out a second time somewhere down the file is exactly the thing
    that made the last repaint a week of work, so the test is about the count,
    not about the colour.
    """
    css = _rules_only(_CSS.read_text(encoding="utf-8"))
    assert css.count("--accent-rgb:") == 1, "the accent is declared more than once"

    triple = re.search(r"--accent-rgb:\s*([0-9]+ [0-9]+ [0-9]+);", css)
    assert triple, "the accent is no longer an rgb triple"
    channels = triple.group(1)
    assert css.count(channels) == 1, "a rule spells the accent out beside the token"

    as_hex = "#" + "".join(f"{int(channel):02x}" for channel in channels.split())
    assert as_hex not in css.lower(), f"{as_hex} is written out somewhere as well"


#: What the accent was before DEC-4, in the three spellings the stylesheet and
#: the page-local <style> blocks used for it.
_OLD_ACCENT: tuple[str, ...] = ("#0071e3", "rgba(0, 113, 227", "rgba(0,113,227", "#0060c0")


def test_no_page_inside_the_shell_keeps_a_copy_of_the_old_accent():
    """The other half of "one line": a page with its own <style> block reaching
    for a colour instead of the token is a module that will not repaint.

    Only pages inside the shell — the ones that link ``app.css`` and can see the
    token. The three self-contained documents (the report, the Pressespiegel,
    the Redaktionsplan) deliberately link no stylesheet, because they are
    forwarded to a client and must not phone home; their palettes are theirs.
    """
    templates = Path(navigation.__file__).resolve().parent / "templates"
    for page in sorted(templates.rglob("*.html")):
        markup = page.read_text(encoding="utf-8")
        standalone = "<!DOCTYPE html>" in markup and "/static/app.css" not in markup
        if standalone:
            continue
        for spelling in _OLD_ACCENT:
            assert spelling not in markup, f"{page.name} paints its own accent"


def test_every_value_in_the_stylesheet_is_declared_in_one_block():
    """One ``:root``. Three of them is how a module ends up owning a colour."""
    css = _CSS.read_text(encoding="utf-8")
    assert len(re.findall(r"^:root\s*\{", css, flags=re.MULTILINE)) == 1


def test_the_repaint_moved_the_shape_tokens_and_not_just_the_colour():
    """DEC-4 asks for a softer radius and a fine shadow, and both are values in
    the block above rather than a number in a rule."""
    css = _CSS.read_text(encoding="utf-8")
    assert re.search(r"--radius:\s*12px;", css)
    assert re.search(r"--radius-s:\s*8px;", css)
    assert re.search(r"--shadow:\s*0 1px 2px", css)
    assert "box-shadow: var(--shadow);" in css
