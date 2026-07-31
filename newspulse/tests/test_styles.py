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
