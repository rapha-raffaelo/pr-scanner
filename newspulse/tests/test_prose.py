"""House style: no dashes in text that leaves the building.

    "bitte speichere dir ab dass du nie '—' benutzt, dann weiss direkt jeder dass
    du KI nutzt um diese Nachrichten zu schreiben"

Enforced rather than requested. The prompt asks; models comply for two paragraphs
and relapse in the third, and nobody reads the third one closely enough to catch
it before it is sent.
"""

from __future__ import annotations

from newspulse.prose import has_dash, plain


def test_a_parenthetical_dash_becomes_a_comma():
    assert plain("Die Anprobe ist gewandert — und wird dort bezahlt.") == (
        "Die Anprobe ist gewandert, und wird dort bezahlt."
    )


def test_the_en_dash_counts_too():
    """Models reach for both, and a reader spots both."""
    assert not has_dash(plain("Zwei Seiten – dieselbe Mechanik."))


def test_a_paragraph_break_survives():
    """The dash at the end of a paragraph must not swallow the break and glue two
    paragraphs into one sentence — a worse artefact than the dash it removes."""
    out = plain("Erster Absatz —\n\nZweiter Absatz beginnt hier.")

    assert out == "Erster Absatz\n\nZweiter Absatz beginnt hier."


def test_a_number_range_is_notation_not_punctuation():
    """"2024–2026" is nobody's stylistic tell, and turning it into a comma would
    change what the sentence says."""
    assert plain("Im Zeitraum 2024–2026 stieg die Quote.") == (
        "Im Zeitraum 2024–2026 stieg die Quote."
    )


def test_hyphenated_words_are_untouched():
    assert plain("KI-Hautpflege bleibt ein Sonderfall.") == (
        "KI-Hautpflege bleibt ein Sonderfall."
    )


def test_a_leading_dash_is_dropped_rather_than_comma_ed():
    assert plain("— Und das heißt: nichts.") == "Und das heißt: nichts."


def test_a_whole_letter_comes_through_clean():
    letter = (
        "Sehr geehrte Frau Faber,\n\n"
        "Sie haben zuletzt beschrieben — sehr treffend — wie es läuft. "
        "Die Rücksendung ist eine Rückmeldung mit Preisschild — schnell, zählbar.\n\n"
        "Mit freundlichen Grüßen\nZalando"
    )

    out = plain(letter)

    assert not has_dash(out)
    assert out.count("\n\n") == 2, "the paragraphs stay paragraphs"
    assert out.endswith("Mit freundlichen Grüßen\nZalando")


def test_no_double_punctuation_is_left_behind():
    assert plain("Das war es — , mehr nicht.") == "Das war es, mehr nicht."
