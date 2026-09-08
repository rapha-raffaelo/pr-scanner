"""Where a style rule lives decides which pages get it.

Twice now the same defect shipped: a component class was declared inside one
template's inline ``<style>``, that page looked right, and every other page
using the same class fell back to the browser's own rendering — most visibly the
"nicht relevant" button, which rendered as a beveled grey UA box in fifty
archive rows while looking correct in settings.

A page-local block is fine for layout only that page has. It is wrong for a
class two pages share, because the second page silently gets nothing. These
tests encode that rule so the next shared class cannot be declared in the wrong
file without a failure.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "newspulse" / "web"
APP_CSS = WEB / "static" / "app.css"
TEMPLATES = sorted((WEB / "templates").rglob("*.html"))

# Controls and surfaces that must work identically wherever they appear. Each
# was, at some point, declared in a single page's <style> and broken elsewhere.
SHARED = [
    "linkbtn",
    "rowlink",
    "notice",
    "panel",
    "cell-name",
    "table-scroll",
    "triage-btn",
    "btn",
]


def _inline_styles(template: Path) -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", template.read_text(), re.S))


def _declares(css: str, cls: str) -> bool:
    """Does ``css`` contain a rule whose subject is ``cls``?

    Only the last compound of a selector counts: ``.sov .linkbtn`` styles the
    class in one container and says nothing about it anywhere else, which is
    exactly the partial fix that let this bug survive its first repair.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for block in re.findall(r"([^{}]+)\{", css):
        for selector in block.split(","):
            selector = selector.strip()
            if not selector or selector.startswith("@"):
                continue
            # Strip pseudo-classes/elements, then take the rightmost compound.
            last = re.split(r"\s+|>|\+|~", selector)[-1]
            last = re.sub(r"::?[a-z-]+(\([^)]*\))?", "", last)
            if cls in re.findall(r"\.([a-zA-Z][\w-]*)", last):
                return True
    return False


@pytest.mark.parametrize("cls", SHARED)
def test_shared_component_is_declared_in_the_global_stylesheet(cls):
    assert _declares(APP_CSS.read_text(), cls), (
        f".{cls} is used across templates but has no rule in app.css. A page-local "
        f"<style> would style it on that page only and leave the browser default "
        f"everywhere else."
    )


@pytest.mark.parametrize("cls", SHARED)
def test_shared_component_is_not_redeclared_in_a_template(cls):
    offenders = [t.name for t in TEMPLATES if _declares(_inline_styles(t), cls)]
    assert not offenders, (
        f".{cls} is declared in {', '.join(offenders)} as well as app.css. Two "
        f"definitions drift; the page-local one wins only on that page."
    )


def test_no_class_used_by_several_pages_is_styled_by_only_one_of_them():
    """The general rule behind the two above.

    If two templates use a class and only one of them declares it, the other
    renders unstyled — the exact shape of this bug. Catching it by rule rather
    than by name means the next one fails here instead of in a screenshot.
    """
    used: dict[str, set[str]] = defaultdict(set)
    declared: dict[str, set[str]] = defaultdict(set)
    for template in TEMPLATES:
        text = template.read_text()
        for cls in re.findall(r"\.([a-zA-Z][\w-]*)", _inline_styles(template)):
            declared[cls].add(template.name)
        markup = re.sub(r"<style>.*?</style>", "", text, flags=re.S)
        for attr in re.findall(r'class="([^"]*)"', markup):
            # Drop Jinja expressions; what is left are literal class names.
            for cls in re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", attr).split():
                used[cls].add(template.name)

    global_css = APP_CSS.read_text()
    stranded = {
        cls: (pages, declared[cls])
        for cls, pages in used.items()
        if len(pages) > 1 and declared[cls] and not _declares(global_css, cls)
        # The declaring page is not the only user: the others get nothing.
        and pages - declared[cls]
    }
    assert not stranded, (
        "These classes are used on several pages but declared inside one page's "
        "<style>, so the other pages render them unstyled: "
        + "; ".join(
            f".{cls} (used by {', '.join(sorted(pages))}; declared in "
            f"{', '.join(sorted(owners))})"
            for cls, (pages, owners) in sorted(stranded.items())
        )
    )


# --- The rail's close button, as a cascade rather than as four separate rules ----
#
# It has been broken twice by merges, both times silently and both times in a
# way no test noticed: the rules were all present and the *relationships*
# between them were not. Once because a stylesheet was taken wholesale and only
# the classes it lacked were appended — which restored an older .rail__e with no
# `position: relative`, leaving the cross nothing to position against. Once
# because the reveal was written as one rule with three selectors and the
# extraction that carried it matched on the leading one, so the two beginning
# `.rail__e` were dropped and only `opacity: 0` survived.


def _app_css() -> str:
    from newspulse.web import app as web_app

    return (web_app._STATIC_DIR / "app.css").read_text("utf-8")


def _rule(css: str, selector: str) -> str:
    import re

    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"{selector} is not declared at all"
    return match.group(1)


def test_the_card_can_position_the_cross_it_contains():
    """The cross is absolutely positioned, so the card has to be its containing
    block. Without this the <details> falls into the card's normal flow and the
    button lands under the label instead of in the corner."""
    css = _app_css()

    assert "position: relative" in _rule(css, ".rail__e")
    assert "position: absolute" in _rule(css, ".rail__x")


def test_the_link_and_not_the_card_carries_the_padding():
    """The card holds a link *and* a form, so it cannot be the anchor. The link
    fills it and keeps the whole box clickable."""
    css = _app_css()

    assert "padding" in _rule(css, ".rail__a")


def test_the_button_starts_hidden_and_something_later_reveals_it():
    """Both halves, and the order between them. `opacity: 0` alone is a button
    nobody can ever see — which is exactly what shipped."""
    css = _app_css()

    assert "opacity: 0" in _rule(css, ".rail__xb")
    hidden = css.index(".rail__xb {")
    revealed = css.index(".rail__e:hover .rail__xb")
    assert revealed > hidden, "the reveal must win the cascade, so it comes after"


def test_the_keyboard_reaches_it_too():
    """Hidden with opacity rather than display:none so it stays focusable, which
    only helps if focus also reveals it."""
    assert ".rail__e:focus-within .rail__xb" in _app_css()


def test_touch_gets_it_permanently_and_only_inside_a_media_query():
    """A touch screen has no pointer to reveal it. The rule that says so must
    stay inside its query: loose, it sets opacity unconditionally and undoes the
    hiding for everyone — which is how it was shipped once."""
    import re

    css = _app_css()

    assert re.search(r"@media \(hover: none\) \{\s*\.rail__xb \{ opacity: 1; \}\s*\}", css)
    assert not re.findall(r"(?m)^\.rail__xb \{[^}]*opacity: 1", css)


def test_each_of_the_rail_rules_is_declared_exactly_once():
    """Two definitions of one rule is one place to forget: the later copy wins,
    and it has twice been the copy still naming a token a repaint removed."""
    import re

    css = _app_css()

    for selector in (".rail__e", ".rail__a", ".rail__x", ".rail__xb"):
        found = re.findall(r"(?m)^" + re.escape(selector) + r"\s*\{", css)
        assert len(found) == 1, f"{selector} is declared {len(found)} times"


# --- Tokens that no longer exist ---------------------------------------------------
#
# An undefined custom property does not fall back to anything: `color:
# var(--gone)` makes the whole declaration invalid and the element keeps
# whatever it inherited. Nothing errors, nothing logs, and the page looks almost
# right — which is how three rules shipped painting nothing at all after a
# repaint removed the tokens they named. The three were the client sign-off
# line, the send confirmation's text, and the shadow under an opened rail card.


def _token_use(css: str) -> set[str]:
    """Every ``var(--x)`` with no fallback. ``var(--x, something)`` is safe by
    construction and is not this test's business."""
    import re

    return {
        m.group(1)
        for m in re.finditer(r"var\(\s*(--[a-z0-9-]+)\s*\)", css)
    }


def test_every_custom_property_a_rule_reads_is_defined_somewhere():
    import re

    css = _app_css()
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))

    missing = sorted(_token_use(css) - defined)

    assert not missing, (
        f"these tokens are read but never defined, so every declaration naming "
        f"one is silently void: {missing}"
    )


def test_a_page_style_block_only_reads_tokens_that_exist():
    """The same rule for the stylesheets that live inside a template: a page
    defining its own token is fine, a page reading one nobody defines is not."""
    import re
    from pathlib import Path

    from newspulse.web import app as web_app

    css = _app_css()
    shared = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    templates = Path(web_app.__file__).parent / "templates"

    broken: dict[str, list[str]] = {}
    for path in sorted(templates.rglob("*.html")):
        blocks = re.findall(r"<style>(.*?)</style>", path.read_text("utf-8"), re.S)
        if not blocks:
            continue
        local = {m for b in blocks for m in re.findall(r"(--[a-z0-9-]+)\s*:", b)}
        used = {m for b in blocks for m in _token_use(b)}
        gap = sorted(used - shared - local)
        if gap:
            broken[path.name] = gap

    assert not broken, f"page styles reading tokens nobody defines: {broken}"


# --- The send stage in two columns ------------------------------------------------
#
# "ich denke diese ansicht sollten wir überarbeiten. am Besten die Kontakte nach
# Rechts und mit einem Vorschlag button (in das Kontaktbuch eintragen)."
#
# The recipient list ran on below the letters. On a stage with three released
# letters that put it a screen and a half down, read only by someone who already
# knew it was there — while it is exactly what the letters are chosen against.


ADVICE = WEB / "templates" / "advice.html"


def _advice() -> str:
    return ADVICE.read_text()


def test_the_recipient_list_is_the_second_column_not_a_footer():
    """The grid must contain both: the letters in a column of their own and the
    list as its sibling. A ``.pitchlist`` left inside ``.send__main`` would look
    identical in the markup diff and render exactly as it did before."""
    body = _advice()
    grid = body[body.index('<div class="send__cols">') : body.index("{# /send__cols #}")]
    main = grid[grid.index('<div class="send__main">') : grid.index("{# /send__main #}")]

    assert '<div class="pitchlist">' in grid
    assert '<div class="pitchlist">' not in main


def test_the_second_column_has_a_width_of_its_own():
    """A grid whose second track is ``auto`` gives the list whatever the longest
    headline in it asks for, which on a row with a long evidence quote is most of
    the stage."""
    css = _inline_styles(ADVICE)
    rule = css[css.index(".send__cols {") :].split("}")[0]

    assert "grid-template-columns" in rule
    # minmax(0, 1fr) on the left, or an unbreakable subject line in a letter card
    # sets the column's min-content width and pushes the list off the page.
    assert "minmax(0, 1fr)" in rule


def test_the_columns_stack_on_a_narrow_screen():
    """Two columns in 360 points is one unreadable column and one sliver."""
    css = _inline_styles(ADVICE)
    narrow = css[css.index("@media (max-width: 900px)") :]

    assert ".send__cols { grid-template-columns: minmax(0, 1fr); }" in narrow
